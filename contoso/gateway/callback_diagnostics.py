"""Short-lived, per-worker callback metadata; never retain OAuth field values."""

from collections import deque
from collections.abc import Mapping
import threading
import time
from urllib.parse import parse_qsl


_FIELDS = frozenset({
    "code", "state", "error", "session_state", "error_description", "error_uri",
    "client_info", "iss", "error_subcode", "error_codes", "suberror",
    "trace_id", "correlation_id", "timestamp",
})
_RECENT = deque(maxlen=32)
_LOCK = threading.Lock()


def record_callback(correlation, method, headers, body, stage, broker_called):
    content_type = ""
    if isinstance(headers, Mapping):
        content_type = next((v for k, v in headers.items()
                             if isinstance(k, str) and k.lower() == "content-type"), "")
    media_type = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
    media_type = (media_type if media_type in {
        "application/x-www-form-urlencoded", "application/json",
    } else "other")
    names = []
    decoded = False
    if isinstance(body, bytes) and len(body) <= 32768:
        try:
            names = [name for name, _ in parse_qsl(
                body.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                encoding="utf-8", errors="strict", max_num_fields=32,
            )]
            decoded = True
        except (ValueError, UnicodeError):
            pass
    metadata = {
        "correlation_id": correlation, "handler": "callback",
        "method": method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} else "OTHER",
        "content_type": media_type, "body_length": len(body) if isinstance(body, bytes) else None,
        "form_decoded": decoded, "field_names": sorted(set(names).intersection(_FIELDS)),
        "unknown_field_count": sum(name not in _FIELDS for name in names),
        "duplicate_fields": len(names) != len(set(names)),
        "has_code": "code" in names, "has_state": "state" in names, "has_error": "error" in names,
        "validation_stage": stage, "broker_called": broker_called,
    }
    with _LOCK:
        _RECENT.append((time.monotonic(), metadata))
    return metadata


def read_callback(correlation):
    with _LOCK:
        for timestamp, metadata in reversed(_RECENT):
            if time.monotonic() - timestamp <= 600 and metadata["correlation_id"] == correlation:
                return {**metadata, "field_names": list(metadata["field_names"])}
    return None
