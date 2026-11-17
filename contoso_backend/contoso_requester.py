"""Authenticated, stateless CONTOSO invocation with RAM-only service credentials."""

import json
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Literal, TypedDict

import requests

if __package__:
    from .config import CONTOSO_DEADLINE_SECONDS, CONTOSO_MIN_REMAINING_MS, CONTOSOSettings, gateway_endpoint
    from .errors import ChatFailure, dependency, invalid
    from .validation import bound_output, unique_object
else:
    from config import CONTOSO_DEADLINE_SECONDS, CONTOSO_MIN_REMAINING_MS, CONTOSOSettings, gateway_endpoint
    from errors import ChatFailure, dependency, invalid
    from validation import bound_output, unique_object

MAX_DOCUMENT_BYTES = 262144
TOKEN_TIMEOUT_SECONDS = 8
TOKEN_EXPIRY_MARGIN = 60
_transport_slot = threading.BoundedSemaphore(1)


class CONTOSOOptions(TypedDict):
    integration: Literal["sdk", "a2a"]
    studio_variant: Literal["standard", "copilot-harness"]


def unavailable():
    return ChatFailure(502, "dependency_unavailable", "The CONTOSO connection is unavailable.")


def text(value, maximum=512):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 and c not in "\n\r\t"
                        for c in value))


def validate_result(value, options: CONTOSOOptions):
    fields = {"status", "answer", "provider_id", "studio_variant", "sources", "citations", "execution_path"}
    if (not isinstance(value, dict) or set(value) != fields
            or value["status"] not in ("answered", "insufficient-evidence", "no-matching-capability")
            or not text(value["answer"], MAX_DOCUMENT_BYTES)
            or not isinstance(value["sources"], list) or len(value["sources"]) > 32
            or not isinstance(value["citations"], list) or len(value["citations"]) > 64
            or not isinstance(value["execution_path"], list)
            or not 1 <= len(value["execution_path"]) <= 32
            or not all(text(item) for item in value["execution_path"])):
        raise dependency()
    if value["status"] == "no-matching-capability":
        if (value["provider_id"] is not None or value["studio_variant"] is not None
                or value["sources"] or value["citations"]):
            raise dependency()
    else:
        if (not text(value["provider_id"], 128)
                or value["studio_variant"] not in (None, options["studio_variant"])
                or value["status"] == "answered" and not value["citations"]):
            raise dependency()
    sources = {}
    for source in value["sources"]:
        if (not isinstance(source, dict) or set(source) != {
                "source_id", "provider_id", "version", "title", "classification"}
                or not all(text(item) for item in source.values())
                or source["provider_id"] != value["provider_id"]
                or source["source_id"] in sources):
            raise dependency()
        sources[source["source_id"]] = source
    cited = set()
    for citation in value["citations"]:
        if (not isinstance(citation, dict) or set(citation) != {"source_id", "version", "section"}
                or not all(text(item) for item in citation.values())):
            raise dependency()
        source = sources.get(citation["source_id"])
        if source is None or citation["version"] != source["version"]:
            raise dependency()
        cited.add(citation["source_id"])
    if cited != set(sources):
        raise dependency()
    answer, limited = bound_output(value["answer"])
    return {"answer": answer, "output_limited": limited,
            "contoso": {name: value[name] for name in fields if name != "answer"}}


class Requester:
    """Preserved JSON-command diagnostic adapter, not an A2A transport."""

    def __init__(self, settings: CONTOSOSettings, session_factory=requests.Session, *,
                 endpoint_path="/api/invoke"):
        self.settings = settings
        if gateway_endpoint(settings.gateway_url, endpoint_path, settings.approved_apim_host) != settings.gateway_url:
            raise ValueError("Invalid CONTOSO endpoint")
        if (settings.requester_client_secret is None) == (settings.requester_certificate is None):
            raise ValueError("Exactly one requester credential is required")
        self.session_factory = session_factory
        self._lock = threading.Lock()
        self._token = None
        self._expires = 0.0

    def invoke(self, question: str, options: CONTOSOOptions, remaining_ms: int):
        if (not text(question, 2000) or len(question.encode("utf-8")) > 8000
                or not isinstance(options, dict) or set(options) != {"integration", "studio_variant"}
                or options["integration"] not in ("sdk", "a2a")
                or options["studio_variant"] not in ("standard", "copilot-harness")):
            raise invalid()
        if remaining_ms < CONTOSO_MIN_REMAINING_MS or not _transport_slot.acquire(blocking=False):
            raise unavailable()
        deadline = time.monotonic() + CONTOSO_DEADLINE_SECONDS
        result = Future()

        def run():
            try:
                with self.session_factory() as session:
                    session.trust_env = False
                    value = self._invoke(session, question, options, deadline)
                result.set_result(value)
            except ChatFailure as error:
                result.set_exception(error)
            except Exception:
                result.set_exception(unavailable())
            finally:
                _transport_slot.release()

        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError:
            _transport_slot.release()
            raise unavailable() from None
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            raise unavailable() from None

    def _token_for(self, session, deadline):
        with self._lock:
            now = time.monotonic()
            certificate = self.settings.requester_certificate
            if certificate is not None:
                try:
                    certificate.require_valid()
                except ValueError:
                    self._token, self._expires = None, 0.0
                    raise unavailable() from None
            if self._token is not None and now < self._expires:
                return self._token
            self._token, self._expires = None, 0.0
            data = {"grant_type": "client_credentials",
                    "client_id": self.settings.requester_client_id,
                    "scope": f"api://{self.settings.gateway_api_client_id}/.default"}
            if certificate is not None:
                try:
                    data.update(
                        client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        client_assertion=certificate.assertion(
                            self.settings.requester_client_id, self.settings.tenant_id,
                        ),
                    )
                except ValueError:
                    raise unavailable() from None
            else:
                data["client_secret"] = self.settings.requester_client_secret
            value = self._post(
                session,
                f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token",
                min(deadline, now + TOKEN_TIMEOUT_SECONDS),
                data=data,
                token_request=True,
            )
            token = value.get("access_token") if isinstance(value, dict) else None
            expires = value.get("expires_in") if isinstance(value, dict) else None
            if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                    or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)
                    or value.get("token_type", "").lower() != "bearer"
                    or type(expires) is not int or not TOKEN_EXPIRY_MARGIN < expires <= 86400):
                raise unavailable()
            self._token = token
            self._expires = now + expires - TOKEN_EXPIRY_MARGIN
            return token

    def _invoke(self, session, question, options, deadline):
        token = self._token_for(session, deadline)
        try:
            value = self._post(session, self.settings.gateway_url, deadline,
                               headers={"Authorization": f"Bearer {token}"},
                               json={"question": question, "studio_variant": options["studio_variant"],
                                     "integration": options["integration"]})
        except ChatFailure:
            # Discard rejected credentials, but never replay an inference request.
            with self._lock:
                if self._token == token:
                    self._token, self._expires = None, 0.0
            raise
        return validate_result(value, options)

    @staticmethod
    def _post(session, url, deadline, token_request=False, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise unavailable()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                   **kwargs.pop("headers", {})}
        with session.post(url, timeout=(min(3, remaining), remaining), headers=headers,
                          allow_redirects=False, stream=True,
                          **kwargs) as response:
            if response.status_code != 200:
                if not token_request and response.status_code == 429:
                    raise ChatFailure(429, "throttled", "The service is busy. Wait before resending.")
                raise unavailable()
            if (response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    != "application/json" or response.headers.get("Content-Encoding", "identity")
                    not in ("", "identity")):
                raise unavailable()
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_DOCUMENT_BYTES):
                raise unavailable()
            raw = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise unavailable()
                chunk = response.raw.read1(min(16384, MAX_DOCUMENT_BYTES + 1 - len(raw)))
                raw.extend(chunk)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    raise unavailable()
                if not chunk:
                    break
            if time.monotonic() >= deadline:
                raise unavailable()
            try:
                return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise unavailable() from None
