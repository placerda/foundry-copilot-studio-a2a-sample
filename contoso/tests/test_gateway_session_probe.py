import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from contoso.gateway_session_probe import snapshot_native_sessions, _observation
from contoso.providers.diagnostics import ERROR_CLASSES, STAGES, STATUSES
from contoso.providers.citations import AttributionReason
from contoso.gateway.tests.test_gateway import block_network


PROJECT = "https://contoso.services.ai.azure.com/api/projects/p"
ENDPOINT = PROJECT + "/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1"
SECRET = "private-mi-secret"
NOW = datetime(2026, 11, 4, 14, 15, tzinfo=timezone.utc).timestamp()
START = NOW - 1800


def invoke(handler, endpoint=ENDPOINT):
    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_token(self, *scopes):
            assert scopes == ("https://ai.azure.com/.default",)
            return SimpleNamespace(token=SECRET, expires_on=time.time() + 3600)

    def credential_factory(**kwargs):
        assert kwargs["retry_total"] == 0
        return Credential()

    def client_factory(**kwargs):
        assert not kwargs["trust_env"] and not kwargs["follow_redirects"]
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return asyncio.run(snapshot_native_sessions(
        SimpleNamespace(project_endpoint=PROJECT), endpoint=endpoint,
        credential_factory=credential_factory, client_factory=client_factory,
        clock=lambda: NOW,
    ))


def row(identifier="own-v9", version="9"):
    return {
        "agent_session_id": identifier, "version_indicator": {"type": "version_ref", "agent_version": version},
        "status": "active", "created_at": START + 60, "last_accessed_at": START + 300,
        "private": SECRET,
    }


def test_own_mi_get_only_v9_console_and_never_raw_console_or_request_content():
    paths = []
    def server(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + SECRET
        paths.append(request.url.path)
        if request.url.path.endswith("/endpoint/sessions"):
            assert request.url.params["limit"] == "10" and request.url.params["order"] == "desc"
            return httpx.Response(200, json={"data": [row(), row("bad-version", "../3"), row("../bad")]})
        assert request.url.path.endswith("/versions/9/sessions/own-v9:logstream")
        messages = [
            "protocol=a2a/1.0 path=/invocations error_class=AccessDenied " + SECRET,
            "CatalogError: Background execution and response storage are disabled " + SECRET,
            'POST /responses HTTP/1.1" 500 Internal Server Error ' + SECRET,
        ]
        body = "".join("event: log\ndata: " + json.dumps({
            "timestamp": "2026-11-04T14:00:00Z", "stream": "stderr", "message": message,
        }) + "\n\n" for message in messages)
        return httpx.Response(200, text=body)
    result = invoke(server)
    assert result["status"] == "session-snapshot" and result["native_a2a_invoked"] is False
    assert len(paths) == 2 and result["matching_recent_session_count"] == 1
    assert result["window_start"] == "2026-11-04T13:45:00+00:00"
    assert result["window_end"] == "2026-11-04T14:15:00+00:00"
    observations = result["sessions"][0]["console"]["observations"]
    assert observations[0]["error_classes"] == ["AccessDenied"]
    assert observations[0]["native_path"] == "/invocations"
    assert observations[1]["guard_markers"] == ["store_or_background_rejected"]
    assert observations[2]["access"]["http_status"] == 500
    assert SECRET not in json.dumps(result) and "message" not in json.dumps(result)


def test_empty_mi_list_is_not_reported_as_no_container_dispatch():
    calls = []
    def server(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [], "has_more": False})
    result = invoke(server)
    assert result["listed_session_count"] == 0 and result["sessions"] == []
    assert result["listing_may_be_truncated"] is False
    assert "does not prove no container dispatch" in result["limitation"] and len(calls) == 1


def test_listing_is_single_page_and_console_count_is_bounded():
    calls = []
    def server(request):
        calls.append(request)
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [row(f"s{i}") for i in range(10)], "has_more": True})
        return httpx.Response(200, text="")
    result = invoke(server)
    assert result["matching_recent_session_count"] == 10 and len(result["sessions"]) == 2
    assert result["has_more"] is True
    assert result["list_page_bound_reached"] is True
    assert result["listing_may_be_truncated"] is True
    assert result["console_selection_truncated"] is True
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["forbidden", "oversize", "duplicate", "redirect", "console-timeout"])
def test_precise_bounded_failure_without_roles_or_fallback(failure):
    def server(request):
        if request.url.path.endswith("/endpoint/sessions"):
            if failure == "forbidden":
                return httpx.Response(403, content=SECRET)
            if failure == "oversize":
                return httpx.Response(200, content=b"x" * 65537)
            if failure == "duplicate":
                return httpx.Response(200, content=b'{"data":[],"data":[]}')
            if failure == "redirect":
                return httpx.Response(302, content=SECRET)
            return httpx.Response(200, json={"data": [row()]})
        raise httpx.ReadTimeout(SECRET)
    result = invoke(server)
    assert SECRET not in json.dumps(result)
    if failure == "forbidden":
        assert result["error"] == {"code": "session_list_http_error", "http_status": 403}
    elif failure == "console-timeout":
        assert result["sessions"][0]["console"]["completion"] == "bounded_timeout"
    else:
        assert result["status"] == "failed"


def test_no_arbitrary_endpoint_can_be_inspected():
    result = invoke(lambda request: pytest.fail("No network permitted"), endpoint="https://attacker.example")
    assert result["error"]["code"] == "invalid_configuration"


def test_only_recent_valid_version_sessions_and_metadata_are_selected():
    stale = row("stale")
    stale["last_accessed_at"] = START - 1
    future = row("future")
    future["created_at"] = NOW + 1
    calls = []

    def server(request):
        calls.append(request)
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [
                stale, future, row("invalid-version", "9/../5"), row(),
            ]})
        messages = [
            {"timestamp": START - 1, "message": "RuntimeError: " + SECRET},
            {"timestamp": NOW + 1, "message": "RuntimeError: " + SECRET},
            {"timestamp": NOW - 60, "message": "RuntimeError: " + SECRET},
        ]
        return httpx.Response(200, text="".join(
            "data: " + json.dumps(item) + "\n\n" for item in messages
        ))

    result = invoke(server)
    assert result["version_selection"] == "actual_session_version_ref"
    assert result["matching_recent_session_count"] == 1 and len(calls) == 2
    assert result["sessions"][0]["session_id"] == "own-v9"
    observations = result["sessions"][0]["console"]["observations"]
    assert len(observations) == 1 and observations[0]["error_classes"] == ["RuntimeError"]
    assert "transient Invocations proxy" in result["limitation"]
    assert SECRET not in json.dumps(result)


def test_actual_session_versions_select_fixed_agent_console_routes():
    paths = []

    def server(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [row("newer", "11"), row("previous", "10")]})
        return httpx.Response(200, text="")

    result = invoke(server)
    assert result["matching_recent_session_count"] == 2
    assert [session["agent_version"] for session in result["sessions"]] == ["11", "10"]
    assert paths[1].endswith("/agents/contoso-foundry-agent/versions/11/sessions/newer:logstream")
    assert paths[2].endswith("/agents/contoso-foundry-agent/versions/10/sessions/previous:logstream")


@pytest.mark.parametrize("version", ["../9", "9?x=1", 9, None, "0", "01", "9" * 11])
def test_malformed_version_metadata_never_becomes_a_console_route(version):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [row("untrusted", version)]})

    result = invoke(server)
    assert result["matching_recent_session_count"] == 0
    assert result["sessions"] == [] and len(calls) == 1


def test_newest_numeric_version_then_created_and_accessed_precede_console_limit():
    rows = [row("newer-v9", "9"), row("older-v10", "10"), row("tie-v10", "10"), row("new-v10", "10")]
    rows[0]["created_at"] = NOW - 1
    rows[1]["created_at"] = START + 10
    rows[2]["created_at"] = START + 120
    rows[2]["last_accessed_at"] = START + 301
    rows[3]["created_at"] = START + 120
    rows[3]["last_accessed_at"] = START + 302
    paths = []

    def server(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(200, text="")

    result = invoke(server)
    assert result["matching_recent_session_count"] == 4
    assert [session["session_id"] for session in result["sessions"]] == ["new-v10", "tie-v10"]
    assert all("/versions/10/" in path for path in paths[1:])
    assert result["console_selection_truncated"] is True
    assert result["console_selection_order"] == "numeric_version_created_accessed_desc"


def test_full_page_without_has_more_still_exposes_possible_truncation():
    def server(request):
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [row(f"s{i}") for i in range(10)], "has_more": False})
        return httpx.Response(200, text="")

    result = invoke(server)
    assert result["has_more"] is False
    assert result["list_page_bound_reached"] is True
    assert result["listing_may_be_truncated"] is True


def stage_observation(**overrides):
    fields = {
        "stage": "broker_http", "provider": "studio-copilot-harness", "status": "response",
        "error_class": "none", "http_status": "200",
    }
    fields.update(overrides)
    message = "2026-11-04 14:00:00,123 INFO contoso.token_client: contoso_stage " + " ".join(
        f"{key}={value}" for key, value in fields.items()
    )
    return _observation(json.dumps({
        "timestamp": NOW - 60, "stream": "stderr", "message": message,
        "headers": SECRET, "token": SECRET,
    }), start=START, end=NOW)


@pytest.mark.parametrize("stage", sorted(STAGES))
@pytest.mark.parametrize("status", sorted(STATUSES))
def test_exact_stage_and_status_allowlists_from_shared_runtime(stage, status):
    observation = stage_observation(stage=stage, status=status)
    assert observation["stage"] == {
        "logger": "contoso.token_client", "stage": stage, "provider": "studio-copilot-harness",
        "status": status, "error_class": "none", "http_status": 200,
    }
    assert SECRET not in json.dumps(observation) and "message" not in observation


@pytest.mark.parametrize("error_class", sorted(ERROR_CLASSES | {"none", "Other"}))
def test_exact_error_class_allowlist_from_shared_runtime(error_class):
    assert stage_observation(error_class=error_class)["stage"]["error_class"] == error_class


@pytest.mark.parametrize("provider", ["studio-copilot-harness", "studio-standard", "knowledge", "unknown"])
@pytest.mark.parametrize("http_status", ["none", "other", "100", "599"])
def test_stage_provider_and_http_metadata_are_fixed_and_bounded(provider, http_status):
    actual = stage_observation(provider=provider, http_status=http_status)["stage"]
    assert actual["provider"] == provider
    assert actual["http_status"] == (int(http_status) if http_status.isdigit() else http_status)


@pytest.mark.parametrize("overrides", [
    {"stage": "secret_stage"}, {"provider": SECRET}, {"status": "secret"},
    {"error_class": SECRET}, {"http_status": "099"}, {"http_status": "600"},
    {"http_status": "200 token=" + SECRET}, {"status": "failed\n" + SECRET},
])
def test_invalid_or_extended_stage_lines_are_discarded_not_legacy_parsed(overrides):
    assert stage_observation(**overrides) is None


@pytest.mark.parametrize("logger", ["contoso.token_client", "contoso.router.runtime", "untrusted.logger"])
def test_real_sdk_prefixed_stage_console_event_only_accepts_fixed_loggers(logger):
    def server(request):
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [row()]})
        message = (
            f"2026-11-04T14:00:00.123Z WARNING {logger}: contoso_stage "
            "stage=provider_transport provider=studio-copilot-harness status=failed "
            "error_class=ReadTimeout http_status=none\n"
        )
        return httpx.Response(200, text="data: " + json.dumps({
            "timestamp": NOW - 60, "stream": "stderr", "message": message,
            "authorization": SECRET,
        }) + "\n\n")

    observations = invoke(server)["sessions"][0]["console"]["observations"]
    if logger == "untrusted.logger":
        assert observations == []
    else:
        assert observations[0]["stage"] == {
            "logger": logger, "stage": "provider_transport", "provider": "studio-copilot-harness",
            "status": "failed", "error_class": "ReadTimeout", "http_status": "none",
        }
    assert SECRET not in json.dumps(observations)


def attribution_message(**overrides):
    fields = {
        "provider": "studio-copilot-harness", "status": "rejected", "reason": "attribution_missing",
        "headers": "0", "known_headers": "0", "version_matches": "0", "headings": "0",
        "known_headings": "0", "id_seen": "no", "version_seen": "no", "section_seen": "no",
        "truncated": "no",
    }
    fields.update(overrides)
    return "2026-11-04 14:00:00,123 WARNING contoso.router.runtime: contoso_attribution " + " ".join(
        f"{key}={value}" for key, value in fields.items()
    )


def attribution_observation(message):
    return _observation(json.dumps({
        "timestamp": NOW - 60, "stream": "stderr", "message": message,
        "headers": SECRET, "body": SECRET,
    }), start=START, end=NOW)


@pytest.mark.parametrize("reason", [reason.value for reason in AttributionReason])
@pytest.mark.parametrize("provider", ["studio-copilot-harness", "studio-standard", "knowledge", "unknown"])
def test_attribution_uses_shared_reason_enum_and_fixed_providers(reason, provider):
    result = attribution_observation(attribution_message(reason=reason, provider=provider))
    assert result["attribution"] == {
        "logger": "contoso.router.runtime", "provider": provider, "status": "rejected",
        "reason": reason, "headers": 0, "known_headers": 0, "version_matches": 0,
        "headings": 0, "known_headings": 0, "id_seen": "no", "version_seen": "no",
        "section_seen": "no", "truncated": "no",
    }
    assert SECRET not in json.dumps(result) and "message" not in result


@pytest.mark.parametrize("field", ["headers", "known_headers", "version_matches", "headings", "known_headings"])
@pytest.mark.parametrize("count", ["0", "1", "9", "10", "19", "20", "29", "30", "31", "32", "many"])
def test_attribution_counts_are_bounded_or_saturated(field, count):
    result = attribution_observation(attribution_message(**{field: count}))
    assert result["attribution"][field] == (count if count == "many" else int(count))


@pytest.mark.parametrize("field", ["id_seen", "version_seen", "section_seen", "truncated"])
@pytest.mark.parametrize("flag", ["yes", "no"])
def test_attribution_presence_flags_remain_lexical_not_attribution_proof(field, flag):
    result = attribution_observation(attribution_message(**{field: flag}))
    assert result["attribution"][field] == flag
    assert result["attribution"]["status"] == "rejected"


@pytest.mark.parametrize("overrides", [
    {"reason": SECRET}, {"reason": "Other"}, {"provider": SECRET}, {"status": "complete"},
    {"headers": "33"}, {"known_headers": "-1"}, {"version_matches": "01"},
    {"headings": "none"}, {"known_headings": "999"}, {"id_seen": "true"},
    {"version_seen": "1"}, {"section_seen": SECRET}, {"truncated": "false"},
    {"reason": "attribution_missing\n" + SECRET},
])
def test_attribution_rejects_unknown_enums_counts_flags_and_injected_text(overrides):
    assert attribution_observation(attribution_message(**overrides)) is None


@pytest.mark.parametrize("transform", [
    lambda text: text + " token=" + SECRET,
    lambda text: text + " reason=source_unknown",
    lambda text: text.replace("WARNING", "INFO"),
    lambda text: text.replace("contoso.router.runtime:", "contoso.token_client:"),
    lambda text: text.replace("contoso.router.runtime:", "untrusted.logger:"),
    lambda text: text + "\nRuntimeError: " + SECRET,
])
def test_attribution_requires_exact_single_default_sdk_line(transform):
    assert attribution_observation(transform(attribution_message())) is None


def test_console_preserves_stage_and_structural_attribution_observations_only():
    def server(request):
        if request.url.path.endswith("/endpoint/sessions"):
            return httpx.Response(200, json={"data": [row("new-runtime", "11")]})
        messages = [
            "2026-11-04T14:00:00.123Z WARNING contoso.router.runtime: contoso_stage "
            "stage=source_attribution provider=studio-copilot-harness status=failed "
            "error_class=AttributionError http_status=none",
            attribution_message(reason="section_missing", headers="1", known_headers="1",
                                version_matches="1", id_seen="yes", version_seen="yes"),
            attribution_message() + " Authorization=" + SECRET,
            "private response text " + SECRET,
        ]
        return httpx.Response(200, text="".join(
            "data: " + json.dumps({"timestamp": NOW - 60, "stream": "stderr", "message": text}) + "\n\n"
            for text in messages
        ))

    observations = invoke(server)["sessions"][0]["console"]["observations"]
    assert len(observations) == 2
    assert observations[0]["stage"]["error_class"] == "AttributionError"
    assert observations[1]["attribution"]["reason"] == "section_missing"
    assert observations[1]["attribution"]["headers"] == 1
    assert SECRET not in json.dumps(observations)
