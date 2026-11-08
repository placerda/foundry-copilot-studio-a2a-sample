"""GET-only recent fixed-Router session evidence in the Gateway MI partition."""

import asyncio
from datetime import datetime, timezone
import json
import math
import re
import time

from azure.identity import ManagedIdentityCredential
import httpx

from contoso.gateway.a2a import _unique
from contoso.gateway_a2a_probe import _safe_correlations, _validate_endpoint
from contoso.providers.citations import AttributionReason
from contoso.providers.diagnostics import ERROR_CLASSES, STAGES, STATUSES


_SERVICE = "contoso-foundry-agent"
_WINDOW_SECONDS = 1800
_PROVIDERS = frozenset({"studio-copilot-harness", "studio-standard", "knowledge", "unknown"})
_LOG_TIMESTAMP = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:\d{2})? "
_STAGE_LINE = re.compile(
    _LOG_TIMESTAMP +
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) "
    r"(?P<logger>contoso\.token_client|contoso\.router\.runtime): contoso_stage "
    r"stage=(?P<stage>[a-z_]{1,32}) provider=(?P<provider>[a-z-]{1,32}) "
    r"status=(?P<status>[a-z]{1,16}) error_class=(?P<error_class>[A-Za-z]{1,64}) "
    r"http_status=(?P<http_status>none|other|[1-5][0-9]{2})"
)
_ATTRIBUTION_REASONS = frozenset(reason.value for reason in AttributionReason)
_COUNT = r"(?:many|[0-9]|[12][0-9]|3[0-2])"
_COUNT_FIELDS = ("headers", "known_headers", "version_matches", "headings", "known_headings")
_ATTRIBUTION_LINE = re.compile(
    _LOG_TIMESTAMP +
    r"WARNING (?P<logger>contoso\.router\.runtime): contoso_attribution "
    r"provider=(?P<provider>[a-z-]{1,32}) status=(?P<status>rejected) "
    r"reason=(?P<reason>[a-z_]{1,64}) "
    + " ".join(rf"{field}=(?P<{field}>{_COUNT})" for field in _COUNT_FIELDS) +
    r" id_seen=(?P<id_seen>yes|no) version_seen=(?P<version_seen>yes|no) "
    r"section_seen=(?P<section_seen>yes|no) truncated=(?P<truncated>yes|no)"
)
_CLASSES = frozenset({
    "AccessDenied", "CatalogError", "ValueError", "TypeError", "RuntimeError",
    "AssertionError", "InternalError", "InvalidParamsError", "ProviderTimeout",
    "UnsupportedOperationError", "KeyError", "AttributeError", "none",
})
_GUARDS = {
    "This demo accepts stateless requests only": "conversation_or_previous_response_rejected",
    "Background execution and response storage are disabled": "store_or_background_rejected",
    "Verified Foundry caller and call context are required": "verified_caller_context_rejected",
    "Native route context is required": "native_route_context_missing",
    "Native cancellation context is required": "native_cancellation_context_missing",
}


def _timestamp(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str) and len(value) <= 64:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except ValueError:
            pass
    return None


def _observation(data, *, start, end):
    """Derive only fixed markers/classes; raw console messages never leave here."""
    try:
        record = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    stamp = _timestamp(record.get("timestamp"))
    message = record.get("message")
    if stamp is None or not start <= stamp <= end or not isinstance(message, str):
        return None
    if len(message) > 8192:
        return None
    metadata = {
        "timestamp": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "stream": record.get("stream") if record.get("stream") in ("stdout", "stderr", "status") else "other",
    }
    if "contoso_attribution" in message:
        matched = _ATTRIBUTION_LINE.fullmatch(message.rstrip("\r\n"))
        if matched is None:
            return None
        fields = matched.groupdict()
        if fields["provider"] not in _PROVIDERS or fields["reason"] not in _ATTRIBUTION_REASONS:
            return None
        for field in _COUNT_FIELDS:
            if fields[field] != "many":
                fields[field] = int(fields[field])
        return {**metadata, "attribution": fields}
    if "contoso_stage" in message:
        matched = _STAGE_LINE.fullmatch(message.rstrip("\r\n"))
        if matched is None:
            return None
        fields = matched.groupdict()
        if (fields["stage"] not in STAGES or fields["status"] not in STATUSES
                or fields["provider"] not in _PROVIDERS
                or fields["error_class"] not in ERROR_CLASSES | {"none", "Other"}):
            return None
        if fields["http_status"].isdigit():
            fields["http_status"] = int(fields["http_status"])
        return {**metadata, "stage": fields}
    classes = set(re.findall(r"\berror_class=([A-Za-z_][A-Za-z0-9_]{0,63})\b", message))
    classes.update(re.findall(r"(?:^|\n)([A-Za-z_][A-Za-z0-9_]{0,63}):", message))
    markers = sorted(value for text, value in _GUARDS.items() if text in message)
    access = re.search(
        r'\b(GET|POST) (/readiness|/responses|/invocations/a2a|/invocations|/a2a) HTTP/[0-9.]+[" ]+([1-5][0-9]{2})\b',
        message,
    )
    native = re.search(r"\bprotocol=a2a/1\.0 path=(/invocations/a2a|/invocations|/a2a)\b", message)
    if not (classes.intersection(_CLASSES) or markers or access or native):
        return None
    return {
        **metadata,
        "error_classes": sorted(classes.intersection(_CLASSES)), "guard_markers": markers,
        "native_path": native[1] if native else None,
        "access": {"method": access[1], "path": access[2], "http_status": int(access[3])} if access else None,
    }


async def _console(client, project, session_id, headers, *, agent_version, start, end):
    evidence = {"http_status": None, "correlations": {}, "observations": [],
                "bytes_read": 0, "events_seen": 0, "completion": "not_started"}
    pending = ""
    try:
        async with asyncio.timeout(8):
            async with client.stream(
                "GET", project + f"/agents/{_SERVICE}/versions/{agent_version}/sessions/{session_id}:logstream",
                params={"api-version": "v1"}, headers={**headers, "Accept": "text/event-stream"},
            ) as response:
                evidence["http_status"] = response.status_code
                evidence["correlations"] = _safe_correlations(response.headers)
                if response.status_code != 200:
                    evidence["completion"] = "upstream_http_error"
                    return evidence
                async for chunk in response.aiter_bytes():
                    if evidence["bytes_read"] + len(chunk) > 65536:
                        evidence["completion"] = "byte_limit"
                        return evidence
                    evidence["bytes_read"] += len(chunk)
                    pending += chunk.decode("utf-8", errors="replace")
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if not line.startswith("data:"):
                            continue
                        evidence["events_seen"] += 1
                        if evidence["events_seen"] > 128:
                            evidence["completion"] = "event_limit"
                            return evidence
                        observation = _observation(line[5:].strip(), start=start, end=end)
                        if observation:
                            evidence["observations"].append(observation)
                    if len(pending) > 16384:
                        evidence["completion"] = "line_limit"
                        return evidence
                evidence["completion"] = "stream_ended"
    except (TimeoutError, httpx.TimeoutException):
        evidence["completion"] = "bounded_timeout"
    except Exception:
        evidence["completion"] = "transport_unavailable"
    return evidence


async def snapshot_native_sessions(
    config, *, endpoint, credential_factory=ManagedIdentityCredential, client_factory=httpx.AsyncClient,
    clock=time.time,
):
    end = clock()
    start = end - _WINDOW_SECONDS
    receipt = {
        "status": "failed", "service": _SERVICE,
        "version_selection": "actual_session_version_ref",
        "native_a2a_invoked": False, "api_version": "v1",
        "window_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "window_end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "list_http_status": None, "correlations": {}, "sessions": [],
        "limits": {"list": 10, "list_pages": 1, "consoles": 2, "console_bytes": 65536, "console_seconds": 8},
        "limitation": (
            "A bounded empty list or absence of markers does not prove no container dispatch. "
            "This session-list API provides no guarantee that transient Invocations proxy calls "
            "create persistent, listable sessions."
        ),
    }
    stage = "configuration"
    try:
        _validate_endpoint(config.project_endpoint, endpoint)
        project = config.project_endpoint
        async with asyncio.timeout(60):
            stage = "credential"
            with credential_factory(connection_timeout=5, read_timeout=10, retry_total=0) as credential:
                token = await asyncio.to_thread(credential.get_token, "https://ai.azure.com/.default")
                if (not isinstance(token.token, str) or not 0 < len(token.token) <= 16384
                        or any(c.isspace() for c in token.token) or token.expires_on <= time.time() + 10):
                    raise ValueError("Invalid managed identity token")
                headers = {"Authorization": "Bearer " + token.token}
                stage = "session_list"
                async with client_factory(
                    timeout=httpx.Timeout(connect=3, read=4, write=3, pool=3),
                    follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "GET", project + f"/agents/{_SERVICE}/endpoint/sessions",
                        params={"api-version": "v1", "limit": 10, "order": "desc"},
                        headers={**headers, "Accept": "application/json"},
                    ) as response:
                        receipt["list_http_status"] = response.status_code
                        receipt["correlations"] = _safe_correlations(response.headers)
                        if response.status_code != 200:
                            return {**receipt, "error": {"code": "session_list_http_error",
                                                        "http_status": response.status_code}}
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > 65536:
                                raise ValueError("Session listing exceeded limit")
                            body.extend(chunk)
                    data = json.loads(body, object_pairs_hook=_unique)
                    rows = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(rows, list) or len(rows) > 10:
                        raise ValueError("Invalid bounded session list")
                    receipt["listed_session_count"] = len(rows)
                    receipt["has_more"] = data.get("has_more") is True
                    receipt["list_page_bound_reached"] = len(rows) == 10
                    receipt["listing_may_be_truncated"] = receipt["has_more"] or len(rows) == 10
                    matches = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        version = row.get("version_indicator")
                        if not isinstance(version, dict) or version.get("type") != "version_ref":
                            continue
                        agent_version = version.get("agent_version")
                        if (not isinstance(agent_version, str)
                                or not re.fullmatch(r"[1-9][0-9]{0,9}", agent_version)):
                            continue
                        identifier = row.get("agent_session_id")
                        created, accessed = _timestamp(row.get("created_at")), _timestamp(row.get("last_accessed_at"))
                        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                                or created is None or accessed is None
                                or created > end or accessed < start):
                            continue
                        matches.append((identifier, agent_version, row, created, accessed))
                    matches.sort(key=lambda match: (int(match[1]), match[3], match[4]), reverse=True)
                    receipt["matching_recent_session_count"] = len(matches)
                    receipt["console_selection_truncated"] = len(matches) > 2
                    receipt["console_selection_order"] = "numeric_version_created_accessed_desc"
                    stage = "session_console"
                    for identifier, agent_version, row, created, accessed in matches[:2]:
                        receipt["sessions"].append({
                            "session_id": identifier, "agent_version": agent_version,
                            "status": row.get("status") if row.get("status") in {
                                "creating", "active", "idle", "updating", "failed", "deleting", "deleted", "expired",
                            } else "other",
                            "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                            "last_accessed_at": datetime.fromtimestamp(accessed, timezone.utc).isoformat(),
                            "console": await _console(
                                client, project, identifier, headers,
                                agent_version=agent_version, start=start, end=end,
                            ),
                        })
                    receipt["status"] = "session-snapshot"
                    return receipt
    except Exception:
        return {**receipt, "error": {"code": {
            "configuration": "invalid_configuration", "credential": "credential_unavailable",
            "session_list": "session_list_unavailable", "session_console": "session_console_unavailable",
        }[stage]}}
