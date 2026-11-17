"""Native Teams channel inside Foundry's authenticated Activity boundary."""

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
import json
import logging
import re
from typing import TypeVar
from urllib.parse import urlsplit

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.agentserver.core import get_request_context, reset_request_context, set_request_context
from microsoft_agents.activity import Activity
from microsoft_agents.hosting.core import Storage, StoreItem, TurnContext
from microsoft_agents.hosting.core.app.typing_indicator import TypingIndicator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from contoso.providers import ProviderError
from .core import AccessDenied, CatalogError
from .hosted import HostedRouter, require_platform_caller
from .native_a2a import _NativeResponsesHost
from .runtime import RuntimeResult

_logger = logging.getLogger(__name__)
_CANCELLATION: ContextVar[asyncio.Event | None] = ContextVar(
    "contoso_activity_cancellation", default=None,
)
TurnWork = Callable[[float], Awaitable[None]]
_DEFER: ContextVar[Callable[[TurnWork], None] | None] = ContextVar(
    "contoso_activity_defer", default=None,
)
_MAX_REQUEST_BYTES = 65536
_MAX_REPLY_BYTES = 24000
_SERVICE_HOSTS = frozenset({"smba.trafficmanager.net"})
StoreItemT = TypeVar("StoreItemT", bound=StoreItem)


class StatelessStorage(Storage):
    """Deliberately discard SDK turn state instead of persisting conversations."""

    async def read(
        self, keys: list[str], *, target_cls: type[StoreItemT], **kwargs,
    ) -> dict[str, StoreItemT]:
        return {}

    async def write(self, changes: dict[str, StoreItem]) -> None:
        pass

    async def delete(self, keys: list[str]) -> None:
        pass


class NativeTeamsHost(_NativeResponsesHost, ActivityAgentServerHost):
    pass


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate Activity field")
        result[key] = value
    return result


class ActivityBoundary:
    """Keep accepted SDK continuations bounded and owned by the ASGI request."""

    def __init__(self, app: ASGIApp, *, overall_timeout: float = 65):
        self.app = app
        self.overall_timeout = overall_timeout
        self.active_turns = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["path"] != "/activity/messages"
                or scope["method"] != "POST"):
            await self.app(scope, receive, send)
            return
        deadline = asyncio.get_running_loop().time() + self.overall_timeout
        body = bytearray()
        try:
            if scope.get("query_string"):
                raise CatalogError("Activity query parameters are disabled")
            headers = dict(scope.get("headers", []))
            if headers.get(b"content-encoding", b"identity") != b"identity":
                raise CatalogError("Encoded Activity requests are disabled")
            async with asyncio.timeout(min(10, self.overall_timeout)):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > _MAX_REQUEST_BYTES:
                        raise CatalogError("Activity request exceeds the byte limit")
                    if not event.get("more_body", False):
                        break
            payload = json.loads(body, object_pairs_hook=_unique_fields)
            if not isinstance(payload, dict):
                raise CatalogError("An Activity object is required")
        except (CatalogError, ValueError, UnicodeError, RecursionError, TimeoutError) as error:
            _logger.warning("protocol=activity outcome=rejected error_class=%s", type(error).__name__)
            await JSONResponse(
                {"error": {"code": "invalid_activity", "message": "Invalid or oversized Activity request."}},
                status_code=400,
            )(scope, receive, send)
            return
        cancellation = asyncio.Event()
        token = _CANCELLATION.set(cancellation)
        delivered = False
        deferred: TurnWork | None = None
        response_complete = False
        response_status = None

        def defer(work: TurnWork) -> None:
            nonlocal deferred
            if deferred is not None or self.active_turns >= 4:
                raise ProviderError("The Teams turn capacity is exhausted")
            if cancellation.is_set():
                raise asyncio.CancelledError
            self.active_turns += 1
            deferred = work

        defer_token = _DEFER.set(defer)

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await cancellation.wait()
            return {"type": "http.disconnect"}

        async def watch_disconnect() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            if deferred is None or not response_complete:
                cancellation.set()

        monitor = asyncio.create_task(watch_disconnect())
        response_started = False

        async def bounded_send(event):
            nonlocal response_started, response_complete, response_status
            if event["type"] == "http.response.start":
                response_started = True
                response_status = event["status"]
            await send(event)
            if event["type"] == "http.response.body" and not event.get("more_body", False):
                response_complete = True

        try:
            async with asyncio.timeout_at(deadline):
                await self.app(scope, replay, bounded_send)
                if deferred is not None:
                    if cancellation.is_set():
                        raise asyncio.CancelledError
                    if not response_complete or response_status != 202:
                        raise ProviderError("The SDK did not acknowledge the accepted Teams turn")
                    _logger.info("protocol=activity outcome=accepted")
                    # The SDK owns the ACK and the fresh Connector context. No task
                    # escapes this request or its total execution/reply deadline.
                    await deferred(deadline - min(2, self.overall_timeout / 10))
        except asyncio.CancelledError:
            _logger.info("protocol=activity outcome=cancelled")
            raise
        except TimeoutError:
            cancellation.set()
            _logger.warning("protocol=activity outcome=failed error_class=TimeoutError")
            if not response_started:
                await JSONResponse(
                    {"error": {"code": "activity_timeout", "message": "Activity request timed out."}},
                    status_code=504,
                )(scope, receive, send)
        except (CatalogError, ProviderError) as error:
            _logger.warning("protocol=activity outcome=failed error_class=%s", type(error).__name__)
            if not response_started:
                raise
        finally:
            cancellation.set()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            if deferred is not None:
                self.active_turns -= 1
            _DEFER.reset(defer_token)
            _CANCELLATION.reset(token)


def _admit(router: HostedRouter, activity: Activity) -> None:
    caller = require_platform_caller(router.context_factory())
    try:
        router.runtime.catalog.require_caller(caller)
    except AccessDenied:
        _logger.warning(
            "protocol=activity outcome=rejected reason=caller_not_approved "
            "platform_context_verified=true platform_caller=%s",
            json.dumps(caller, ensure_ascii=True),
        )
        raise
    if str(activity.channel_id) != "msteams":
        raise AccessDenied("This Activity route accepts Teams only")
    try:
        service = urlsplit(activity.service_url or "")
        port = service.port
    except ValueError:
        raise AccessDenied("An approved Teams reply service is required") from None
    if (service.scheme != "https" or service.hostname not in _SERVICE_HOSTS
            or port not in (None, 443) or service.username or service.password
            or service.query or service.fragment):
        raise AccessDenied("An approved Teams reply service is required")


def render_result(result: RuntimeResult) -> str:
    if len(result.answer.split()) > 150:
        raise ProviderError("Teams answer exceeds the word limit")
    answer = result.answer
    if result.result is not None:
        sections = sorted({section for source in result.result.sources for section in source.sections})
        if sections:
            headings = "|".join(re.escape(section) for section in sections)
            answer = re.sub(
                r"\s*(\*\*(?:" + headings + r")\*\*)\s*", r"\n\n\1\n\n", answer,
            ).strip()
    if len(answer.split()) > 150:
        raise ProviderError("Teams answer exceeds the word limit")
    if result.result is None:
        lines = [answer, "", "Sources:", "No sources returned."]
    else:
        sections_by_source: dict[tuple[str, str, str], list[str]] = {
            (source.source_id, source.provider_id, source.version): []
            for source in result.result.sources
        }
        for citation in result.result.response.citations:
            key = (citation.source_id, citation.provider_id, citation.version)
            if key not in sections_by_source:
                raise ProviderError("Teams citation has no matching source")
            sections_by_source[key].append(citation.section)
        lines = [answer, "", "**References**"]
        for source in result.result.sources:
            lines.extend([
                "", f"{source.title} (version {source.version})",
                f"Source ID: `{source.source_id}`",
            ])
            sections = sections_by_source[(source.source_id, source.provider_id, source.version)]
            if sections:
                lines.extend(["", "Sections cited: " + ", ".join(sections)])
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > _MAX_REPLY_BYTES:
        raise ProviderError("Teams reply exceeds the byte limit")
    return text


def configure_teams(host: NativeTeamsHost, router: HostedRouter) -> None:
    host.add_middleware(ActivityBoundary, overall_timeout=router.overall_timeout)
    app = host.agent_app

    @app.before_turn
    async def admit(context: TurnContext, state) -> bool:
        _admit(router, context.activity)
        return True

    @app.activity("message", channel_id="msteams")
    async def message(context: TurnContext, state) -> None:
        cancellation = _CANCELLATION.get()
        defer = _DEFER.get()
        if cancellation is None or defer is None or context.identity is None:
            raise AccessDenied("The bounded Activity request context is required")
        activity = context.activity
        # Teams can include an inline HTML rendering of the same text.
        if any(
            not isinstance(attachment.content_type, str)
            or attachment.content_type.lower() != "text/html"
            or attachment.content_url is not None
            for attachment in activity.attachments or ()
        ):
            raise CatalogError("Only a text question is supported")
        question = TurnContext.remove_recipient_mention(activity)
        if (not isinstance(question, str) or not question.strip()
                or len(question) > 2000 or len(question.encode("utf-8")) > 8000
                or question.lstrip().startswith("{")):
            raise CatalogError("A bounded plain-text question is required")
        platform_context = get_request_context()
        continuation = activity.model_copy(deep=True)
        identity = context.identity
        adapter = context.adapter

        async def work(work_deadline: float) -> None:
            async def deliver(reply_context: TurnContext) -> None:
                async with TypingIndicator(reply_context):
                    try:
                        async with asyncio.timeout_at(work_deadline):
                            result = await router.answer({"input": question}, cancellation)
                            text = render_result(result)
                    except (CatalogError, ProviderError, TimeoutError):
                        await reply_context.send_activity(Activity(
                            type="message", text_format="plain",
                            text="Unable to answer this request. No business approval or booking was made.",
                        ))
                        raise
                    if cancellation.is_set():
                        raise asyncio.CancelledError
                    await reply_context.send_activity(Activity(
                        type="message", text=text, text_format="markdown",
                    ))
                    _logger.info("protocol=activity outcome=completed route_status=%s", result.status)

            context_token = set_request_context(platform_context)
            try:
                await adapter.continue_conversation_with_claims(identity, continuation, deliver)
            finally:
                reset_request_context(context_token)

        defer(work)

    @app.error
    async def error(context: TurnContext, exception: Exception) -> None:
        _logger.warning("protocol=activity outcome=failed error_class=%s", type(exception).__name__)
        raise exception

    adapter = host.adapter
    if adapter is None:
        raise CatalogError("The native Teams channel adapter is required")
    # The SDK default acknowledges failed turns after logging them.
    adapter.on_turn_error = error
