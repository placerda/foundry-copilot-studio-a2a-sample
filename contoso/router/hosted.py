"""Hosted runtime adapters. Local headers are not deployment evidence."""

import asyncio
from collections.abc import Callable, Mapping
import json
from typing import Protocol

from contoso.providers import ProviderTimeout
from .core import AccessDenied, Catalog, CatalogError, STUDIOVariant
from .runtime import RuntimeResult


class PlatformContext(Protocol):
    user_id: str | None

    def platform_headers(self) -> dict[str, str]: ...


class RoutingRuntime(Protocol):
    catalog: Catalog

    async def run(
        self, question: str, *, caller_id: str, variant: str | None = None,
    ) -> RuntimeResult: ...


def current_platform_context() -> PlatformContext:
    from azure.ai.agentserver.core import get_request_context

    return get_request_context()


def require_platform_caller(context: PlatformContext) -> str:
    caller = context.user_id
    headers = context.platform_headers()
    call_id = headers.get("x-agent-foundry-call-id")
    if (not isinstance(caller, str) or not caller.strip() or len(caller) > 8192
            or any(ord(c) < 32 for c in caller)
            or set(headers) != {"x-agent-foundry-call-id"}
            or not isinstance(call_id, str) or not call_id
            or len(call_id) > 16384
            or any(c.isspace() or ord(c) < 32 for c in call_id)):
        raise AccessDenied("Verified Foundry caller and call context are required")
    return caller


def parse_input(request: Mapping) -> tuple[str, str | None]:
    if not isinstance(request, Mapping):
        raise CatalogError("A CONTOSO request object is required")
    if request.get("previous_response_id") or request.get("conversation"):
        raise CatalogError("This demo accepts stateless requests only")
    if request.get("background") or request.get("store"):
        raise CatalogError("Background execution and response storage are disabled")
    text = request.get("input")
    if isinstance(text, list):
        if len(text) != 1 or not isinstance(text[0], Mapping) or text[0].get("role") != "user":
            raise CatalogError("Exactly one user message is required")
        text = text[0].get("content")
        if isinstance(text, list):
            if (len(text) != 1 or not isinstance(text[0], Mapping)
                    or text[0].get("type") != "input_text"):
                raise CatalogError("Exactly one input-text part is required")
            text = text[0].get("text")
    if not isinstance(text, str) or len(text) > 12000:
        raise CatalogError("A bounded text input is required")
    if not text.lstrip().startswith("{"):
        return text, None
    try:
        envelope = json.loads(text, object_pairs_hook=_unique_fields)
    except json.JSONDecodeError:
        raise CatalogError("Invalid CONTOSO request envelope") from None
    if (not isinstance(envelope, dict)
            or set(envelope) != {"question", "studio_variant"}
            or not isinstance(envelope["question"], str)
            or (envelope["studio_variant"] is not None
                and not isinstance(envelope["studio_variant"], str))):
        raise CatalogError("The CONTOSO envelope requires only question and studio_variant")
    return envelope["question"], envelope["studio_variant"]


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate CONTOSO envelope field")
        result[key] = value
    return result


class HostedRouter:
    def __init__(
        self,
        runtime: RoutingRuntime,
        *,
        context_factory: Callable[[], PlatformContext] = current_platform_context,
        overall_timeout: float = 75,
        fixed_variant: STUDIOVariant | None = None,
    ):
        if (type(overall_timeout) not in (int, float)
                or not 0 < overall_timeout <= 120):
            raise CatalogError("Hosted request timeout must be between 0 and 120 seconds")
        if fixed_variant is not None and not isinstance(fixed_variant, STUDIOVariant):
            raise CatalogError("The fixed STUDIO variant must be administrator configured")
        self.runtime = runtime
        self.context_factory = context_factory
        self.overall_timeout = overall_timeout
        self.fixed_variant = fixed_variant

    async def answer(self, request: Mapping, cancellation: asyncio.Event) -> RuntimeResult:
        caller = require_platform_caller(self.context_factory())
        self.runtime.catalog.require_caller(caller)
        if cancellation.is_set():
            raise asyncio.CancelledError
        question, variant = parse_input(request)
        if self.fixed_variant is not None:
            if variant is not None and variant != self.fixed_variant.value:
                raise CatalogError("The requested STUDIO variant is not available in this Hosted deployment")
            variant = self.fixed_variant.value
        work = asyncio.create_task(self.runtime.run(question, caller_id=caller, variant=variant))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (work, cancelled), timeout=self.overall_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            if work not in done:
                raise ProviderTimeout("The Hosted request exceeded its time budget")
            return await work
        finally:
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

    def build_host(self, *, teams_router: "HostedRouter | None" = None):
        from azure.ai.agentserver.responses import (
            CreateResponse, ResponseContext, TextResponse,
        )
        from .native_a2a import build_native_host

        async def native_answer(request, cancellation):
            result = await self.answer(request, cancellation)
            return json.dumps(result.to_dict(), ensure_ascii=True)

        host = build_native_host(
            native_answer, name="CONTOSO Router",
            description="Routes approved questions and verifies configured source attribution.",
            overall_timeout=self.overall_timeout,
            teams_router=teams_router,
        )

        @host.response_handler
        async def handler(
            request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event,
        ):
            result = await self.answer(request, cancellation_signal)
            return TextResponse(
                context, request, text=json.dumps(result.to_dict(), ensure_ascii=True),
            )

        return host
