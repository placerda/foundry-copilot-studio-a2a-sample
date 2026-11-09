"""A2A 1.0 requester for the authenticated, protocol-preserving Gateway bridge.

The bridge's native Hosted endpoint still requires live acceptance before CONTOSO
enablement. JSON-command and Responses adapters are never used as fallbacks.
"""

import json
from dataclasses import replace
from uuid import uuid4

import requests

if __package__:
    from .contoso_requester import CONTOSOOptions, MAX_DOCUMENT_BYTES, Requester, text, unavailable, validate_result
    from .config import CONTOSOSettings
    from .errors import invalid
    from .validation import unique_object
else:
    from contoso_requester import CONTOSOOptions, MAX_DOCUMENT_BYTES, Requester, text, unavailable, validate_result
    from config import CONTOSOSettings
    from errors import invalid
    from validation import unique_object

NATIVE_OPTIONS: CONTOSOOptions = {"integration": "a2a", "studio_variant": "copilot-harness"}


def _parts(value):
    if (not isinstance(value, dict) or not isinstance(value.get("parts"), list)
            or not 1 <= len(value["parts"]) <= 32):
        raise unavailable()
    parts = []
    for part in value["parts"]:
        if (not isinstance(part, dict) or set(part) != {"text"}
                or not text(part["text"], MAX_DOCUMENT_BYTES)):
            raise unavailable()
        parts.append(part["text"])
    return parts


def parse_a2a_result(value, request_id):
    if (not isinstance(value, dict) or set(value) != {"jsonrpc", "id", "result"}
            or value["jsonrpc"] != "2.0" or value["id"] != request_id
            or not isinstance(value["result"], dict)):
        raise unavailable()
    result = value["result"]
    if set(result) == {"message"}:
        message = result["message"]
        if isinstance(message, dict) and message.get("role", "ROLE_AGENT") != "ROLE_AGENT":
            raise unavailable()
        parts = _parts(message)
    elif set(result) == {"task"}:
        task = result["task"]
        if (not isinstance(task, dict) or not isinstance(task.get("status"), dict)
                or task["status"].get("state") != "TASK_STATE_COMPLETED"
                or not isinstance(task.get("artifacts"), list)
                or not 1 <= len(task["artifacts"]) <= 16):
            raise unavailable()
        parts = [part for artifact in task["artifacts"] for part in _parts(artifact)]
    else:
        raise unavailable()
    raw = "\n".join(parts)
    if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise unavailable()
    try:
        runtime = json.loads(raw, object_pairs_hook=unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise unavailable() from None
    validated = validate_result(runtime, NATIVE_OPTIONS)
    if (validated["contoso"]["status"] != "no-matching-capability"
            and validated["contoso"]["studio_variant"] != "copilot-harness"):
        raise unavailable()
    return validated


class A2ARequester(Requester):
    def __init__(self, settings: CONTOSOSettings, session_factory=requests.Session):
        if settings.a2a_gateway_url is None:
            raise ValueError("The native A2A Gateway endpoint is required")
        super().__init__(replace(settings, gateway_url=settings.a2a_gateway_url),
                         session_factory, endpoint_path="/api/a2a")

    def invoke(self, question: str, options: CONTOSOOptions, remaining_ms: int):
        if options != NATIVE_OPTIONS:
            raise invalid()
        return super().invoke(question, options, remaining_ms)

    def _invoke(self, session, question, options, deadline):
        token = self._token_for(session, deadline)
        request_id = str(uuid4())
        payload = {
            "jsonrpc": "2.0", "id": request_id, "method": "SendMessage",
            "params": {
                "message": {"messageId": str(uuid4()), "role": "ROLE_USER",
                            "parts": [{"text": question}]},
                "configuration": {"acceptedOutputModes": ["text/plain"],
                                  "returnImmediately": False},
            },
        }
        try:
            value = self._post(session, self.settings.gateway_url, deadline,
                               headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
                               json=payload)
        except Exception:
            with self._lock:
                self._token, self._expires = None, 0.0
            raise
        return parse_a2a_result(value, request_id)
