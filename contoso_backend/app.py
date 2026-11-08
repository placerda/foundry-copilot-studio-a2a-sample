import json
import logging
import time
from uuid import uuid4

from langsmith import tracing_context

from errors import ChatFailure
from config import Settings
from validation import decode_transport, headers_of, validate_messages, validate_contoso_options

logger = logging.getLogger("contoso_chat")
logger.setLevel(logging.INFO)
_services = None


def handle(event, context, settings, authorizer, graph, requester=None):
    request_id = getattr(context, "aws_request_id", None) or str(uuid4())
    start = time.monotonic()
    status = 500
    code = "internal_error"
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method")
        if method not in ("POST", "OPTIONS"):
            raise ChatFailure(405, "method_not_allowed", "Use POST to send messages.")
        if event.get("rawPath", "/") != "/":
            raise ChatFailure(400, "unsupported_path", "Unsupported path.")
        if method == "OPTIONS":
            status, code = 200, "preflight"
            return response(status, {})
        authorizer.verify(headers_of(event).get("authorization"))
        payload = decode_transport(event)
        messages = validate_messages(payload, settings)
        options = validate_contoso_options(payload, settings)
        with tracing_context(enabled=False):
            if options is not None:
                if requester is None:
                    raise ChatFailure(502, "dependency_unavailable", "The CONTOSO connection is unavailable.")
                state = requester.invoke(messages[-1]["content"], options,
                                         context.get_remaining_time_in_millis())
            else:
                state = graph.invoke({"messages": messages,
                                      "remaining_ms": context.get_remaining_time_in_millis()})
        status, code = 200, "success"
        return response(status, {"answer": state["answer"], "request_id": request_id,
                                 "output_limited": state["output_limited"],
                                 **({"contoso": state["contoso"]} if options is not None else {})})
    except ChatFailure as error:
        status, code = error.status, error.code
        return failure_response(error, request_id)
    except Exception:
        return failure_response(ChatFailure(500, "internal_error", "Internal error. Try again manually."),
                                request_id)
    finally:
        logger.info(json.dumps({"request_id": request_id, "status": status, "code": code,
                                "duration_ms": round((time.monotonic() - start) * 1000)}))


def lambda_handler(event, context):
    global _services
    try:
        if _services is None:
            from auth import Authorizer, KeyCache
            from bedrock_client import Model
            from graph import build_graph
            from a2a_requester import A2ARequester
            settings = Settings.from_env()
            _services = (settings, Authorizer(settings, KeyCache(settings)), build_graph(Model(settings)),
                         A2ARequester(settings.contoso) if settings.contoso is not None else None)
        return handle(event, context, *_services)
    except Exception:
        request_id = getattr(context, "aws_request_id", None) or str(uuid4())
        logger.error(json.dumps({"request_id": request_id, "status": 500, "code": "configuration_error"}))
        return failure_response(ChatFailure(500, "internal_error", "The service is not configured correctly."), request_id)


def response(status, payload, headers=None):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json; charset=utf-8",
                        "Cache-Control": "no-store", **(headers or {})},
            "body": json.dumps(payload, ensure_ascii=True)}


def failure_response(error: ChatFailure, request_id):
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
    if error.status == 405:
        headers["Allow"] = "POST, OPTIONS"
    return response(error.status, {"error": {"code": error.code,
                    "message": error.message}, "request_id": request_id}, headers)
