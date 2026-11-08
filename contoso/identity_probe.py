"""Explicit bootstrap version. No business question or private source is served."""

import asyncio
import json

from contoso.router.core import AccessDenied
from contoso.router.hosted import current_platform_context, parse_input, require_platform_caller


def probe_identity(request, cancellation_signal, *, context_factory=current_platform_context):
    platform = context_factory()
    caller = require_platform_caller(platform)
    question, variant = parse_input(request)
    if question != "CONTOSO identity probe" or variant is not None:
        raise AccessDenied("This identity-bootstrap version cannot serve business requests")
    if cancellation_signal.is_set():
        raise asyncio.CancelledError
    return {
        "status": "identity-bootstrap",
        "platform_caller_id": caller,
        "platform_call_id": platform.platform_headers()["x-agent-foundry-call-id"],
        "knowledge_access": False,
    }


def build_probe_host(*, context_factory=current_platform_context):
    async def resolve(request, cancellation_signal):
        return probe_identity(
            request, cancellation_signal, context_factory=context_factory,
        )

    return build_json_probe_host(resolve)


def parse_probe_reply(text):
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("Invalid identity-bootstrap response size")
    result = json.loads(text)
    if (
        not isinstance(result, dict)
        or set(result) != {
            "status", "platform_caller_id", "platform_call_id", "knowledge_access",
        }
        or result["status"] != "identity-bootstrap"
        or result["knowledge_access"] is not False
    ):
        raise ValueError("The service is not an identity-bootstrap deployment")
    _check_platform_fields(result)
    return result


def parse_link_reply(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError("Invalid diagnostic-link response size")
    result = json.loads(text)
    if (
        not isinstance(result, dict)
        or set(result) != {
            "status", "platform_caller_id", "platform_call_id", "knowledge_access", "downstream",
        }
        or result["status"] != "identity-link-bootstrap"
        or result["knowledge_access"] is not False
    ):
        raise ValueError("The service is not a diagnostic-link deployment")
    _check_platform_fields(result)
    parse_probe_reply(json.dumps(result["downstream"], ensure_ascii=True))
    return result


def _check_platform_fields(result):
    for field in ("platform_caller_id", "platform_call_id"):
        value = result[field]
        if (
            not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("Invalid authenticated platform identity")


def build_json_probe_host(resolve):
    from azure.ai.agentserver.responses import (
        CreateResponse, ResponseContext, TextResponse,
    )
    from contoso.router.native_a2a import build_native_host

    async def native_answer(request, cancellation_signal):
        payload = await resolve(request, cancellation_signal)
        return json.dumps(payload, ensure_ascii=True)

    host = build_native_host(
        native_answer, name="CONTOSO identity diagnostic",
        description="Reports verified platform context without accessing business knowledge.",
    )

    @host.response_handler
    async def handler(
        request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event,
    ):
        payload = await resolve(request, cancellation_signal)
        return TextResponse(context, request, text=json.dumps(payload, ensure_ascii=True))

    return host
