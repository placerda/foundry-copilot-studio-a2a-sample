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


def build_catalog_probe_host(client, *, allowed_callers, context_factory=current_platform_context):
    if not allowed_callers:
        raise ValueError("The catalog probe requires approved platform callers")

    async def resolve(request, cancellation_signal):
        caller = require_platform_caller(context_factory())
        question, variant = parse_input(request)
        if (caller not in allowed_callers or question != "CONTOSO catalog access probe"
                or variant is not None):
            raise AccessDenied("This diagnostic version accepts only its authorized catalog probe")
        if cancellation_signal.is_set():
            raise asyncio.CancelledError
        work = asyncio.create_task(client.list_apis())
        cancelled = asyncio.create_task(cancellation_signal.wait())
        try:
            done, _ = await asyncio.wait(
                (work, cancelled), return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            records = await work
            return {
                "status": "catalog-access-proof",
                "inventory_count": len(records),
                "knowledge_access": False,
            }
        finally:
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

    return build_json_probe_host(resolve)


def build_apim_probe_host(credential, policy, *, allowed_callers,
                          context_factory=current_platform_context):
    import threading
    import time

    from contoso.bounded import run_bounded_sync
    from contoso.providers import A2ATransport, TransportError

    if not allowed_callers:
        raise ValueError("The APIM probe requires approved platform callers")
    probes = {
        "CONTOSO logistics A2A access probe": (
            "motors-logistics",
            "In the synthetic Contoso Motors demo, how should receiving and returnable "
            "racks and boxes be coordinated for the pending proposal of 300 units Friday "
            "and 200 Monday? Do not execute actions. Cite the source ID, Version 1 "
            "and the source section names. Answer in at most 150 words.",
        ),
        "CONTOSO studio A2A access probe": (
            "studio-copilot-harness",
            "What exact penalty will the buyer charge for delivering the remaining 200 units Monday?",
        ),
    }
    token_slot = threading.BoundedSemaphore(1)
    http_slot = threading.BoundedSemaphore(1)

    async def invoke(provider, question):
        def acquire():
            token = credential.get_token(next(iter(policy.auth.scopes)))
            if token.expires_on <= time.time() + 60:
                raise TransportError("The APIM probe credential is too close to expiry")
            return token.token

        async with asyncio.timeout(8):
            token = await run_bounded_sync(token_slot, acquire)
        transport = A2ATransport(
            endpoint=policy.endpoint(provider, "1"),
            approved_origins=frozenset({policy.origin}),
            connect_timeout=3, read_timeout=30, overall_timeout=40,
            max_response_bytes=131072, worker_slot=http_slot,
        )
        return {
            "status": "apim-a2a-access-proof", "provider_id": provider,
            "protocol_version": "1.0", "output": await transport.invoke(question, token),
        }

    async def resolve(request, cancellation_signal):
        caller = require_platform_caller(context_factory())
        question, variant = parse_input(request)
        if caller not in allowed_callers or question not in probes or variant is not None:
            raise AccessDenied("This diagnostic version accepts only its authorized APIM probes")
        if cancellation_signal.is_set():
            raise asyncio.CancelledError
        work = asyncio.create_task(invoke(*probes[question]))
        cancelled = asyncio.create_task(cancellation_signal.wait())
        try:
            async with asyncio.timeout(50):
                done, _ = await asyncio.wait(
                    (work, cancelled), return_when=asyncio.FIRST_COMPLETED,
                )
                if cancelled in done:
                    raise asyncio.CancelledError
                return await work
        finally:
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

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
