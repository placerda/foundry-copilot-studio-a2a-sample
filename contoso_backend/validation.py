import base64
import binascii
import json
import re

from config import MAX_BODY_BYTES, MAX_CONTENT_LENGTH
from errors import ChatFailure, invalid


def headers_of(event):
    headers = event.get("headers") or {}
    if not isinstance(headers, dict):
        raise invalid()
    normalized = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise invalid()
        lower = name.lower()
        if lower in normalized:
            raise invalid()
        normalized[lower] = value
    return normalized


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def decode_transport(event):
    headers = headers_of(event)
    media = headers.get("content-type", "")
    if not re.fullmatch(r"application/json(?:\s*;\s*charset=utf-8)?", media, flags=re.IGNORECASE):
        raise invalid()
    if "content-encoding" in headers:
        raise invalid()
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(body, str) or type(encoded) is not bool:
        raise invalid()
    try:
        if encoded:
            if len(body) > 4 * ((MAX_BODY_BYTES + 2) // 3):
                raise ValueError
            raw = base64.b64decode(body, validate=True)
        else:
            if len(body) > MAX_BODY_BYTES:
                raise ValueError
            raw = body.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(payload, dict) or set(payload) not in (
                {"messages"}, {"messages", "contoso_options"}):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise invalid() from None
    return payload


def validate_messages(payload, settings):
    if not isinstance(payload, dict) or set(payload) not in (
            {"messages"}, {"messages", "contoso_options"}):
        raise invalid()
    messages = payload["messages"]
    if (not isinstance(messages, list) or not 1 <= len(messages) <= settings.max_messages
            or len(messages) % 2 != 1):
        raise invalid()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise invalid()
        text = message["content"]
        if (message["role"] != ("user" if index % 2 == 0 else "assistant")
                or not isinstance(text, str) or not text.strip()
                or len(text) > MAX_CONTENT_LENGTH
                or any(0xD800 <= ord(char) <= 0xDFFF for char in text)):
            raise invalid()
    return messages


def validate_contoso_options(payload, settings):
    if "contoso_options" not in payload:
        return None
    options = payload["contoso_options"]
    if (not isinstance(options, dict) or set(options) != {"integration", "studio_variant"}
            or not isinstance(options["integration"], str)
            or options["integration"] not in ("sdk", "a2a")
            or not isinstance(options["studio_variant"], str)
            or options["studio_variant"] not in ("standard", "copilot-harness")):
        raise invalid()
    if settings.contoso is None:
        raise ChatFailure(502, "dependency_unavailable", "The CONTOSO connection is unavailable.")
    question = payload["messages"][-1]["content"]
    if len(question) > 2000 or len(question.encode("utf-8")) > 8000:
        raise invalid()
    return options


def bound_output(answer):
    # Match the existing history text limit; flag rather than silently truncate.
    limited = len(answer) > MAX_CONTENT_LENGTH
    return answer[:MAX_CONTENT_LENGTH], limited
