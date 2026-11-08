"""Authenticated A2A 1.0 relay over one explicitly selected Hosted transport.

This is not a Responses adapter. Request IDs, method and message framing stay
unchanged; only the transport credential changes at the private-network bridge.
Native is the default; explicit HTTP passthrough uses only the invocations root.
"""

import asyncio
from collections.abc import Callable
import json
from uuid import UUID

import httpx

from contoso.connections import ConnectionUnavailable
from contoso.providers.a2a import _parse
from .http import validate_runtime_result
from .hosted_transport import select_hosted_a2a_endpoint


def _unique(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("Invalid A2A framing")
        payload[key] = value
    return payload


def validate_a2a_request(payload) -> dict:
    try:
        if (not isinstance(payload, dict) or set(payload) != {"jsonrpc", "id", "method", "params"}
                or payload["jsonrpc"] != "2.0" or payload["method"] != "SendMessage"):
            raise ValueError
        if not isinstance(payload["id"], str) or str(UUID(payload["id"])) != payload["id"]:
            raise ValueError
        params = payload["params"]
        if not isinstance(params, dict) or set(params) != {"message", "configuration"}:
            raise ValueError
        if params["configuration"] != {
            "acceptedOutputModes": ["text/plain"], "returnImmediately": False,
        } or params["configuration"]["returnImmediately"] is not False:
            raise ValueError
        message = params["message"]
        if (not isinstance(message, dict) or set(message) != {"messageId", "role", "parts"}
                or message["role"] != "ROLE_USER"
                or not isinstance(message["messageId"], str)
                or str(UUID(message["messageId"])) != message["messageId"]
                or not isinstance(message["parts"], list) or len(message["parts"]) != 1
                or not isinstance(message["parts"][0], dict) or set(message["parts"][0]) != {"text"}):
            raise ValueError
        question = message["parts"][0]["text"]
        if (not isinstance(question, str) or not question.strip() or len(question) > 2000
                or len(question.encode("utf-8", errors="strict")) > 8000
                or any(ord(c) < 32 and c not in "\t\r\n" for c in question)):
            raise ValueError
        return payload
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise ValueError("Invalid A2A request.") from None


class HostedA2ABridge:
    def __init__(
        self, credential, *, endpoint: str,
        client_factory: Callable = httpx.AsyncClient,
        hosting_transport: str = "native",
    ):
        selected = select_hosted_a2a_endpoint(
            endpoint, hosting_transport=hosting_transport,
        )
        self.credential = credential
        self.endpoint = selected
        self.client_factory = client_factory
        self.hosting_transport = hosting_transport

    async def forward(self, payload: dict) -> dict:
        validate_a2a_request(payload)
        try:
            token = await asyncio.to_thread(self.credential.get_token, "https://ai.azure.com/.default")
            async with self.client_factory(
                timeout=httpx.Timeout(connect=5, read=70, write=10, pool=5),
                follow_redirects=False, trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", self.endpoint, json=payload,
                    headers={"Authorization": "Bearer " + token.token, "A2A-Version": "1.0"},
                ) as response:
                    if response.status_code != 200:
                        raise ValueError
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1048576:
                            raise ValueError
            result = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or result.get("id") != payload["id"]
                    or set(result) not in (
                        {"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"},
                    )):
                raise ValueError
            if "error" in result:
                raise ValueError
            text, _ = _parse(result, payload["id"])
            validated = validate_runtime_result(json.loads(text, object_pairs_hook=_unique))
            if (validated["status"] == "unavailable"
                    or (validated["status"] != "no-matching-capability"
                        and (validated["provider_id"] != "studio-copilot-harness"
                             or validated["studio_variant"] != "copilot-harness"))):
                raise ValueError
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectionUnavailable("The selected native Hosted A2A service is unavailable") from None
