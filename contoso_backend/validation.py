import json
import re

if __package__:
    from .config import MAX_BODY_BYTES, MAX_CONTENT_LENGTH
    from .errors import ChatFailure, invalid
else:
    from config import MAX_BODY_BYTES, MAX_CONTENT_LENGTH
    from errors import ChatFailure, invalid


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def decode_json(raw, headers, allow_identity_mode=False):
    media = headers.get("content-type", "")
    if (not re.fullmatch(r"application/json(?:\s*;\s*charset=utf-8)?", media, re.IGNORECASE)
            or "content-encoding" in headers or len(raw) > MAX_BODY_BYTES):
        raise invalid()
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(payload, dict):
            raise ValueError
        base_fields = set(payload) - ({"identity_mode"} if allow_identity_mode else set())
        if base_fields not in (
                {"messages"}, {"messages", "contoso_options"}):
            raise ValueError
        return payload
    except (ValueError, UnicodeError, RecursionError):
        raise invalid() from None


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
