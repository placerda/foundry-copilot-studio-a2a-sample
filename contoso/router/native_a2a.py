"""Stateless A2A hosting, including Hosted HTTP passthrough; SDK identity only."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from enum import Enum
import json
import logging
import os
from typing import NoReturn

# These switches must precede importing the native SDK and creating its host.
os.environ["OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"] = "false"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"

from a2a.helpers import new_text_message
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Message, SendMessageRequest
from a2a.utils.errors import InternalError, InvalidParamsError, UnsupportedOperationError
from azure.ai.agentserver.a2a import A2AAgentServerHost
from azure.ai.agentserver.responses import (
    InMemoryResponseProvider, ResponsesAgentServerHost, ResponsesServerOptions,
)
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from contoso.providers import ProviderError
from .core import AccessDenied, CatalogError
from .hosted import current_platform_context, require_platform_caller

_PASSTHROUGH_PATH = "/invocations"
_SDK_PASSTHROUGH_PATH = "/invocations/a2a"
_RPC_PATHS = frozenset({"/a2a", _SDK_PASSTHROUGH_PATH, _PASSTHROUGH_PATH})
_CANCELLATION = "contoso_native_cancellation"
_REQUEST_PATH = "contoso_native_path"
_MAX_REQUEST_BYTES = 16384
_MAX_REPLY_BYTES = 1048576
_READ_TIMEOUT = 10
_logger = logging.getLogger(__name__)
Answer = Callable[[Mapping[str, object], asyncio.Event], Awaitable[str]]


class _Rejection(Enum):
    ENVELOPE = "envelope"
    PARAMS_FIELDS = "params_fields"
    MESSAGE = "message"
    TEXT = "text"
    CONFIGURATION = "configuration"
    DUPLICATE_FIELD = "duplicate_field"
    NUMERIC_LIMIT = "numeric_limit"
    REQUEST_SIZE = "request_size"
    PROTOCOL_CONFLICT = "protocol_conflict"


class _InputRejected(InvalidParamsError):
    def __init__(self, reason: _Rejection):
        super().__init__("Invalid A2A request")
        self.reason = reason


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _InputRejected(_Rejection.DUPLICATE_FIELD)
        result[key] = value
    return result


def _bounded_integer(value: str) -> int:
    if len(value) > 16:
        raise _InputRejected(_Rejection.NUMERIC_LIMIT)
    result = int(value)
    if abs(result) > 9007199254740991:
        raise _InputRejected(_Rejection.NUMERIC_LIMIT)
    return result


def _protocol_versions(scope: Scope) -> set[str]:
    request = Request(scope)
    return {
        value.strip() for value in [
            *request.headers.getlist("a2a-version"),
            *(value for key, value in request.query_params.multi_items()
              if key.lower() == "a2a-version"),
        ] if value.strip()
    }


class _InvocationsAlias:
    """Alias the raw HTTP path before the SDK binds its real platform context."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["path"] == _PASSTHROUGH_PATH
                and scope["method"] == "POST"):
            scope = {
                **scope, "path": _SDK_PASSTHROUGH_PATH,
                "raw_path": _SDK_PASSTHROUGH_PATH.encode("ascii"),
            }
            if not _protocol_versions(scope):
                # This passthrough route's server contract is A2A 1.0, not 0.3.
                scope["headers"] = [*scope["headers"], (b"a2a-version", b"1.0")]
        await self.app(scope, receive, send)


def _validate_envelope(value: object) -> None:
    """Keep demo bounds while accepting omitted protobuf configuration defaults."""
    if (not isinstance(value, dict)
            or set(value) != {"jsonrpc", "id", "method", "params"}
            or value["jsonrpc"] != "2.0"
            or type(value["id"]) not in (str, int)
            or isinstance(value["id"], str) and not 0 < len(value["id"]) <= 128):
        raise _InputRejected(_Rejection.ENVELOPE)
    if value["method"] != "SendMessage":
        raise UnsupportedOperationError("Only stateless SendMessage is supported")
    params = value["params"]
    if (not isinstance(params, dict) or "message" not in params
            or not set(params).issubset({"message", "configuration"})):
        raise _InputRejected(_Rejection.PARAMS_FIELDS)
    message = params["message"]
    if (not isinstance(message, dict)
            or set(message) != {"messageId", "role", "parts"}
            or not isinstance(message["messageId"], str)
            or not 0 < len(message["messageId"]) <= 128
            or message["role"] != "ROLE_USER"
            or not isinstance(message["parts"], list) or len(message["parts"]) != 1):
        raise _InputRejected(_Rejection.MESSAGE)
    part = message["parts"][0]
    if (not isinstance(part, dict) or set(part) != {"text"}
            or not isinstance(part["text"], str) or not part["text"].strip()
            or len(part["text"]) > 2000 or len(part["text"].encode("utf-8")) > 8000
            or part["text"].lstrip().startswith("{")):
        raise _InputRejected(_Rejection.TEXT)
    configuration = params.get("configuration", {})
    if (not isinstance(configuration, dict)
            or not set(configuration).issubset({"acceptedOutputModes", "returnImmediately"})
            or configuration.get("acceptedOutputModes", []) not in ([], ["text/plain"])
            or configuration.get("returnImmediately", False) is not False):
        raise _InputRejected(_Rejection.CONFIGURATION)


class _Boundary:
    """Bound and validate wire input before SDK decoding; propagate disconnects."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["path"] not in _RPC_PATHS
                or scope["method"] != "POST"):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        request_id: str | int | None = None
        try:
            async with asyncio.timeout(_READ_TIMEOUT):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > _MAX_REQUEST_BYTES:
                        raise _InputRejected(_Rejection.REQUEST_SIZE)
                    if not event.get("more_body", False):
                        break
            value = json.loads(body, object_pairs_hook=_unique_fields, parse_int=_bounded_integer)
            if isinstance(value, dict) and type(value.get("id")) in (str, int):
                candidate = value["id"]
                if isinstance(candidate, int) or len(candidate) <= 128:
                    request_id = candidate
            _validate_envelope(value)
            versions = _protocol_versions(scope)
            if len(versions) > 1:
                raise _InputRejected(_Rejection.PROTOCOL_CONFLICT)
            if versions != {"1.0"} and not (scope["path"] == _PASSTHROUGH_PATH and not versions):
                await self._reject(
                    scope, receive, send, request_id, -32009,
                    "A2A version 1.0 is required", "VersionNotSupportedError",
                )
                return
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            await self._reject(scope, receive, send, request_id, -32700, "Invalid JSON", "JSONParseError")
            return
        except TimeoutError:
            await self._reject(scope, receive, send, request_id, -32602, "Request timed out", "TimeoutError")
            return
        except InvalidParamsError as error:
            await self._reject(
                scope, receive, send, request_id, -32602, "Invalid A2A request", "InvalidParamsError",
                error.reason if isinstance(error, _InputRejected) else None,
            )
            return
        except UnsupportedOperationError:
            await self._reject(
                scope, receive, send, request_id, -32004,
                "Only stateless SendMessage is supported", "UnsupportedOperationError",
            )
            return

        cancellation = asyncio.Event()
        _logger.info("protocol=a2a/1.0 path=%s error_class=none", scope["path"])
        scope.setdefault("state", {})[_CANCELLATION] = cancellation
        scope["state"][_REQUEST_PATH] = scope["path"]
        delivered = False

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
            cancellation.set()

        monitor = asyncio.create_task(watch_disconnect())
        try:
            await self.app(scope, replay, send)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, request_id: str | int | None,
        code: int, message: str, error_class: str, reason: _Rejection | None = None,
    ) -> None:
        _logger.warning("protocol=a2a/1.0 path=%s error_class=%s", scope["path"], error_class)
        error: dict[str, object] = {"code": code, "message": message}
        if reason is not None:
            error["data"] = {"reason": reason.value}
        response = JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "error": error,
        }, status_code=400)
        await response(scope, receive, send)


class _CallContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state[_CANCELLATION] = request.scope["state"][_CANCELLATION]
        context.state[_REQUEST_PATH] = request.scope["state"][_REQUEST_PATH]
        return context


class StatelessHandler(RequestHandler):
    def __init__(self, answer: Answer, *, overall_timeout: float = 75):
        if (type(overall_timeout) not in (int, float)
                or not 0 < overall_timeout <= 120):
            raise CatalogError("Native A2A timeout must be between 0 and 120 seconds")
        self.answer = answer
        self.overall_timeout = overall_timeout

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message:
        cancellation = context.state.get(_CANCELLATION)
        if not isinstance(cancellation, asyncio.Event):
            raise InternalError("Native cancellation context is required")
        path = context.state.get(_REQUEST_PATH)
        if not isinstance(path, str) or path not in _RPC_PATHS:
            raise InternalError("Native route context is required")
        try:
            require_platform_caller(current_platform_context())
            if cancellation.is_set():
                raise asyncio.CancelledError
            work = asyncio.create_task(self.answer({"input": params.message.parts[0].text}, cancellation))
            cancelled = asyncio.create_task(cancellation.wait())
            try:
                async with asyncio.timeout(self.overall_timeout):
                    done, _ = await asyncio.wait(
                        (work, cancelled), return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancelled in done:
                        raise asyncio.CancelledError
                    text = await work
            finally:
                work.cancel()
                cancelled.cancel()
                await asyncio.gather(work, cancelled, return_exceptions=True)
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > _MAX_REPLY_BYTES:
                raise InternalError("Native A2A output exceeds its size limit or is empty")
            return new_text_message(text)
        except (AccessDenied, CatalogError) as error:
            _logger.warning("protocol=a2a/1.0 path=%s error_class=%s", path, type(error).__name__)
            raise InvalidParamsError("The request or platform caller is not authorized") from None
        except (ProviderError, TimeoutError) as error:
            _logger.warning("protocol=a2a/1.0 path=%s error_class=%s", path, type(error).__name__)
            raise InternalError("The CONTOSO request could not be completed") from None

    async def _unsupported(self, params: object, context: ServerCallContext) -> NoReturn:
        raise UnsupportedOperationError("Only stateless SendMessage is supported")

    async def _unsupported_stream(
        self, params: object, context: ServerCallContext,
    ) -> AsyncGenerator[NoReturn, None]:
        raise UnsupportedOperationError("Streaming and task subscription are disabled")
        yield

    on_get_task = _unsupported
    on_list_tasks = _unsupported
    on_cancel_task = _unsupported
    on_create_task_push_notification_config = _unsupported
    on_get_task_push_notification_config = _unsupported
    on_list_task_push_notification_configs = _unsupported
    on_delete_task_push_notification_config = _unsupported
    on_get_extended_agent_card = _unsupported
    on_message_send_stream = _unsupported_stream
    on_subscribe_to_task = _unsupported_stream


class _NativeResponsesHost(A2AAgentServerHost, ResponsesAgentServerHost):
    pass


def build_native_host(
    answer: Answer, *, name: str, description: str, overall_timeout: float = 75,
) -> _NativeResponsesHost:
    """Expose SDK A2A through direct routes and Hosted HTTP passthrough.

    `/invocations` is explicitly A2A 1.0 when no selector survives forwarding;
    incompatible or conflicting selectors still reject. Only the ASGI path and
    absent protocol selector change, never payload bytes or platform identity.
    Existing native routes still require a selector. This does not repair the
    managed public IncomingA2A frontend. Responses diagnostics remain separate.
    """
    card = AgentCard(
        name=name, description=description, version="1.0.0",
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(),
        supported_interfaces=[AgentInterface(
            protocol_binding="JSONRPC", protocol_version="1.0", url="http://localhost:8088/a2a",
        )],
        skills=[AgentSkill(
            id="contoso-stateless", name=name, description=description, tags=["contoso"],
            input_modes=["text/plain"], output_modes=["text/plain"],
        )],
    )
    host = _NativeResponsesHost(
        agent_card=card, request_handler=StatelessHandler(answer, overall_timeout=overall_timeout),
        context_builder=_CallContextBuilder(),
        store=InMemoryResponseProvider(),
        options=ResponsesServerOptions(
            default_fetch_history_count=1, resilient_background=False, steerable_conversations=False,
        ),
    )
    host.add_middleware(_InvocationsAlias)
    host.add_middleware(_Boundary)
    return host
