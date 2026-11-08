"""Managed service credentials and bounded HTTPS access to the private cache broker."""

from collections.abc import Callable
import json
import logging
import threading
import time
from typing import Protocol

import requests
from azure.core.exceptions import AzureError

from contoso.bounded import run_bounded_sync
from contoso.router.core import Capability, CatalogEntry, CatalogError, _https_origin
from contoso.router.runtime import ScopedToken
from contoso.providers import ProtocolError, TransportError
from contoso.providers.diagnostics import log_stage


_logger = logging.getLogger(__name__)


class Credential(Protocol):
    def get_token(self, *scopes): ...


class ProviderTokens:
    def __init__(
        self,
        credential: Credential,
        *,
        tenant_id: str,
        gateway_url: str,
        gateway_api_id: str,
        session_factory: Callable = requests.Session,
    ):
        if _https_origin(gateway_url) != gateway_url.rstrip("/"):
            raise CatalogError("The configured token gateway must be an HTTPS origin")
        self.credential = credential
        self.tenant_id = tenant_id
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_api_id = gateway_api_id
        self.session_factory = session_factory
        self._slot = threading.BoundedSemaphore(1)

    def _get(self, entry: CatalogEntry) -> ScopedToken:
        stage = "service_credential"
        try:
            log_stage(_logger, entry, stage, "start")
            if entry.capability == Capability.KNOWLEDGE_KNOWLEDGE:
                token = self.credential.get_token("https://ai.azure.com/.default")
                scoped = ScopedToken(
                    token.token, self.tenant_id, entry.auth.audience,
                    entry.auth.scopes, token.expires_on,
                )
                log_stage(_logger, entry, stage, "complete")
                return scoped
            bearer = self.credential.get_token(f"api://{self.gateway_api_id}/.default")
            log_stage(_logger, entry, stage, "complete")
            stage = "broker_http"
            log_stage(_logger, entry, stage, "start")
            with self.session_factory() as session:
                session.trust_env = False
                with session.post(
                    self.gateway_url + "/api/connections/token",
                    json={"provider_id": entry.provider_id},
                    headers={"Authorization": f"Bearer {bearer.token}", "Accept": "application/json"},
                    timeout=(5, 15), allow_redirects=False, stream=True,
                ) as response:
                    log_stage(_logger, entry, stage, "response", http_status=response.status_code)
                    if response.status_code != 200:
                        raise TransportError("The authorized provider connection is unavailable")
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=4096):
                        content.extend(chunk)
                        if len(content) > 32768:
                            raise ProtocolError("The token broker response exceeded its byte limit")
                    log_stage(_logger, entry, stage, "complete")
                    stage = "broker_metadata"
                    log_stage(_logger, entry, stage, "start")
                    try:
                        payload = json.loads(content, object_pairs_hook=_unique_fields)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise ProtocolError("The token broker returned invalid JSON") from None
            if (not isinstance(payload, dict)
                    or set(payload) != {"token", "tenant_id", "audience", "scopes", "expires_at"}):
                raise ProtocolError("The token broker returned invalid token metadata")
            token = ScopedToken(**payload)
            token.require(entry, time.time() + 60)
            log_stage(_logger, entry, stage, "complete")
            return token
        except (AzureError, requests.RequestException) as error:
            log_stage(_logger, entry, stage, "failed", error=error)
            raise TransportError("The configured service credential or token broker is unavailable") from None
        except (CatalogError, ProtocolError, TransportError) as error:
            log_stage(_logger, entry, stage, "failed", error=error)
            raise

    async def __call__(self, entry: CatalogEntry) -> ScopedToken:
        return await run_bounded_sync(self._slot, lambda: self._get(entry))


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("The token broker returned duplicate fields")
        result[key] = value
    return result
