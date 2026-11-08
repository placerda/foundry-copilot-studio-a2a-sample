"""Encrypted, identity-bound OAuth state with conditional Blob operations."""

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
import time
from typing import Protocol, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class StateError(RuntimeError):
    pass


class StateMissing(StateError):
    pass


class StateConflict(StateError):
    pass


class StateUnavailable(StateError):
    pass


class BlobBackend(Protocol):
    def read(self, name: str, max_bytes: int) -> tuple[bytes, str]: ...
    def write(self, name: str, data: bytes, etag: str | None) -> str: ...
    def delete(self, name: str, etag: str) -> None: ...


@dataclass(frozen=True)
class StateRecord:
    payload: dict = field(repr=False)
    etag: str

    def __post_init__(self):
        if (not isinstance(self.etag, str) or not self.etag or len(self.etag) > 128
                or any(c.isspace() or ord(c) < 32 for c in self.etag)):
            raise StateError("State storage returned an invalid conditional version")


class EncryptedStateStore:
    def __init__(
        self, contoso_backend: BlobBackend, encoded_key: str, *,
        namespace: str, clock: Callable[[], float] = time.time,
        max_bytes: int = 524288,
    ):
        try:
            raw_key = base64.b64decode(encoded_key, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise StateError("Invalid state encryption configuration") from None
        if (len(raw_key) != 32 or not isinstance(namespace, str) or not namespace.strip()
                or type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576):
            raise StateError("Invalid state encryption configuration")
        keys = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=None,
            info=b"CONTOSO encrypted OAuth state v1",
        ).derive(raw_key)
        self._cipher = AESGCM(keys[:32])
        self._name_key = keys[32:]
        self.contoso_backend = contoso_backend
        self.namespace = namespace
        self.clock = clock
        self.max_bytes = max_bytes

    def _binding(self, purpose: str, identity: str) -> tuple[str, bytes]:
        if (purpose not in ("oauth-flow", "oauth-cache")
                or not isinstance(identity, str) or not identity.strip()
                or len(identity) > 8192 or any(ord(c) < 32 for c in identity)):
            raise StateError("Invalid state binding")
        aad = json.dumps(
            [self.namespace, purpose, identity], separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        digest = hmac.new(self._name_key, aad, hashlib.sha256).hexdigest()
        return f"v1/{purpose}/{digest}", aad

    def put(
        self, purpose: str, identity: str, payload: dict, *,
        ttl: float, etag: str | None = None,
    ) -> StateRecord:
        name, aad = self._binding(purpose, identity)
        if (type(ttl) not in (int, float) or not math.isfinite(ttl)
                or not 0 < ttl <= 2592000 or not isinstance(payload, dict)):
            raise StateError("Invalid state payload or lifetime")
        if etag is not None and (not isinstance(etag, str) or not etag):
            raise StateError("Invalid conditional state version")
        try:
            clear = json.dumps(
                {"expires": self.clock() + ttl, "payload": payload},
                separators=(",", ":"), ensure_ascii=True, allow_nan=False,
            ).encode("ascii")
        except (ValueError, TypeError, RecursionError):
            raise StateError("State payload must be valid JSON") from None
        if len(clear) > self.max_bytes - 32:
            raise StateError("State payload exceeds the configured limit")
        nonce = os.urandom(12)
        encrypted = b"CTS1" + nonce + self._cipher.encrypt(nonce, clear, aad)
        version = self.contoso_backend.write(name, encrypted, etag)
        return StateRecord(payload, version)

    def get(self, purpose: str, identity: str) -> StateRecord:
        name, aad = self._binding(purpose, identity)
        encrypted, etag = self.contoso_backend.read(name, self.max_bytes)
        if (not isinstance(encrypted, bytes) or not 32 <= len(encrypted) <= self.max_bytes
                or encrypted[:4] != b"CTS1"):
            raise StateError("Stored state is invalid")
        try:
            clear = self._cipher.decrypt(encrypted[4:16], encrypted[16:], aad)
            envelope = json.loads(clear)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError):
            raise StateError("Stored state failed integrity verification") from None
        if (not isinstance(envelope, dict) or set(envelope) != {"expires", "payload"}
                or type(envelope["expires"]) not in (int, float)
                or not math.isfinite(envelope["expires"])
                or not isinstance(envelope["payload"], dict)):
            raise StateError("Stored state has an invalid schema")
        if envelope["expires"] <= self.clock():
            raise StateMissing("Stored OAuth state has expired")
        return StateRecord(envelope["payload"], etag)

    def consume(self, purpose: str, identity: str) -> dict:
        if purpose != "oauth-flow":
            raise StateError("Only OAuth flows can be consumed")
        record = self.get(purpose, identity)
        name, _ = self._binding(purpose, identity)
        self.contoso_backend.delete(name, record.etag)
        return record.payload


T = TypeVar("T")


class AzureBlobBackend:
    """The injected SDK container uses managed identity and private networking."""

    def __init__(self, container):
        self.container = container

    @staticmethod
    def _call(operation: Callable[[], T]) -> T:
        from azure.core.exceptions import (
            AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError,
        )

        try:
            return operation()
        except ResourceNotFoundError:
            raise StateMissing("The requested OAuth connection is not available") from None
        except (ResourceExistsError, ResourceModifiedError):
            raise StateConflict("OAuth state changed concurrently") from None
        except AzureError:
            raise StateUnavailable("Private OAuth state storage is unavailable") from None

    def read(self, name: str, max_bytes: int) -> tuple[bytes, str]:
        def operation():
            blob = self.container.get_blob_client(name)
            properties = blob.get_blob_properties()
            if properties.size > max_bytes:
                raise StateError("Stored OAuth state exceeds the configured limit")
            from azure.core import MatchConditions

            data = blob.download_blob(
                etag=properties.etag, match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            ).readall()
            return data, properties.etag
        return self._call(operation)

    def write(self, name: str, data: bytes, etag: str | None) -> str:
        def operation():
            from azure.core import MatchConditions
            from azure.storage.blob import ContentSettings

            options = {
                "overwrite": etag is not None,
                "content_settings": ContentSettings(content_type="application/octet-stream"),
            }
            if etag is not None:
                options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
            result = self.container.get_blob_client(name).upload_blob(data, **options)
            return result["etag"]
        return self._call(operation)

    def delete(self, name: str, etag: str) -> None:
        def operation():
            from azure.core import MatchConditions

            self.container.get_blob_client(name).delete_blob(
                etag=etag, match_condition=MatchConditions.IfNotModified,
            )
        self._call(operation)
