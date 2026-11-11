import copy
import io
import json
import threading
import time

import pytest
import requests

from contoso_requester import Requester, MAX_DOCUMENT_BYTES, validate_result
from config import Settings
from errors import ChatFailure, unauthenticated
from contoso_backend.local_server import ChatService
from contoso_backend.tests.test_api import FakeAuth
from validation import validate_contoso_options

TENANT = "44444444-4444-4444-4444-444444444444"
API = "55555555-5555-5555-5555-555555555555"
CLIENT = "66666666-6666-6666-6666-666666666666"
SECRET = "PROTECTED-SENTINEL"
SERVICE_TOKEN = "service-credential-token"
OPTIONS = {"integration": "sdk", "studio_variant": "standard"}


@pytest.fixture
def contoso_env(environment):
    return {**environment, "CONTOSO_ENABLED": "true",
            "CONTOSO_GATEWAY_URL": "https://gateway.synthetic.azurewebsites.net",
            "CONTOSO_ENTRA_TENANT_ID": TENANT, "CONTOSO_GATEWAY_API_CLIENT_ID": API,
            "CONTOSO_REQUESTER_CLIENT_ID": CLIENT, "CONTOSO_REQUESTER_CLIENT_SECRET": SECRET}


def runtime_result(variant="standard", status="answered"):
    return {"status": status, "answer": "Grounded synthetic example.",
            "provider_id": "consumer-provider", "studio_variant": variant,
            "sources": [{"source_id": "source-1", "provider_id": "consumer-provider",
                         "version": "v1", "title": "Synthetic insight",
                         "classification": "Synthetic demonstration data"}],
            "citations": [{"source_id": "source-1", "version": "v1", "section": "Evidence limits"}],
            "execution_path": ["Verified gateway", "Actual integration", "consumer-provider via sdk-d2e",
                               "Verified source attribution"]}


def token_result(**changes):
    return {"access_token": SERVICE_TOKEN, "token_type": "Bearer", "expires_in": 3600, **changes}


class FakeResponse:
    def __init__(self, value=None, status=200, raw=None, headers=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.raw = io.BytesIO(raw if raw is not None else json.dumps(value).encode())
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def post(self, url, **kwargs):
        assert self.trust_env is False
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def requester(contoso_env, responses):
    session = FakeSession(responses)
    return Requester(Settings.from_env(contoso_env).contoso, lambda: session), session


@pytest.mark.parametrize("integration", ["sdk", "a2a"])
@pytest.mark.parametrize("variant", ["standard", "copilot-harness"])
def test_real_two_hop_payload_and_no_user_token(contoso_env, integration, variant, caplog):
    result = runtime_result(variant)
    worker, session = requester(contoso_env, [FakeResponse(token_result()), FakeResponse(result)])
    options = {"integration": integration, "studio_variant": variant}
    body = worker.invoke("latest question", options, 90000)
    assert body["answer"] == result["answer"]
    assert body["output_limited"] is False
    assert body["contoso"] == {key: value for key, value in result.items() if key != "answer"}
    token_url, token_args = session.calls[0]
    assert token_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert token_args["data"] == {"grant_type": "client_credentials", "client_id": CLIENT,
                                  "client_secret": SECRET, "scope": f"api://{API}/.default"}
    gateway_url, gateway_args = session.calls[1]
    assert gateway_url == "https://gateway.synthetic.azurewebsites.net/api/invoke"
    assert gateway_args["json"] == {"question": "latest question", **options}
    assert gateway_args["headers"]["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    for _, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["timeout"][0] <= 3
        assert kwargs["timeout"][1] <= 75
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert session.calls[0][1]["timeout"][1] <= 8
    for private in (SECRET, SERVICE_TOKEN, TENANT, API, CLIENT):
        assert private not in json.dumps(body) + caplog.text
    assert SECRET not in repr(Settings.from_env(contoso_env))


@pytest.mark.parametrize("url", [
    "http://gateway.azurewebsites.net", "https://example.com", "https://127.0.0.1",
    "https://gateway.azurewebsites.net:443", "https://user@gateway.azurewebsites.net",
    "https://gateway.azurewebsites.net?x=1", "https://gateway.azurewebsites.net?",
    "https://gateway.azurewebsites.net#x", "https://gateway.azurewebsites.net#",
    "https://gateway.azurewebsites.net/invoke", "https://gateway.azurewebsites.net/api/invoke/",
    "https://gateway.azurewebsites.net/%2e", "https://gateway.azurewebsites.net\\x",
    " https://gateway.azurewebsites.net", "https://gateway.azurewebsites.net.evil.com",
])
def test_trusted_config_endpoint_only(contoso_env, url):
    with pytest.raises(ValueError, match="Invalid service configuration"):
        Settings.from_env({**contoso_env, "CONTOSO_GATEWAY_URL": url})


@pytest.mark.parametrize("suffix", ["", "/", "/api/invoke"])
def test_endpoint_canonicalized(contoso_env, suffix):
    config = Settings.from_env({**contoso_env, "CONTOSO_GATEWAY_URL": "https://gateway.azurewebsites.net" + suffix})
    assert config.contoso.gateway_url == "https://gateway.azurewebsites.net/api/invoke"


@pytest.mark.parametrize("field", [
    "CONTOSO_GATEWAY_URL", "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_GATEWAY_API_CLIENT_ID",
    "CONTOSO_REQUESTER_CLIENT_ID", "CONTOSO_REQUESTER_CLIENT_SECRET",
])
def test_enabled_configuration_requires_all_fields(contoso_env, field):
    with pytest.raises(ValueError):
        Settings.from_env({**contoso_env, field: ""})


@pytest.mark.parametrize("flag", ["TRUE", "False", "1", "", True, None])
def test_flag_exact_string_bool(contoso_env, flag):
    with pytest.raises(ValueError):
        Settings.from_env({**contoso_env, "CONTOSO_ENABLED": flag})


def test_disabled_connection_cannot_invoke_a_requester(contoso_env):
    settings = Settings.from_env({**contoso_env, "CONTOSO_ENABLED": "false", "CONTOSO_GATEWAY_URL": "not a URL"})
    assert settings.contoso is None
    payload = {"messages": [{"role": "user", "content": "q"}], "contoso_options": OPTIONS}
    with pytest.raises(ChatFailure) as failure:
        validate_contoso_options(payload, settings)
    assert failure.value.status == 502


@pytest.mark.parametrize("options", [None, {}, "sdk", [], {"integration": "sdk"},
    {"integration": "sdk", "studio_variant": "other"}, {"integration": "unknown", "studio_variant": "standard"},
    {**OPTIONS, "gateway_url": "https://evil.com"}, {**OPTIONS, "caller_id": "other"},
    {"integration": [], "studio_variant": "standard"}])
def test_options_strict_and_no_fallback(contoso_env, options):
    worker, session = requester(contoso_env, [])
    payload = {"messages": [{"role": "user", "content": "q"}], "contoso_options": options}
    with pytest.raises(ChatFailure) as failure:
        validate_contoso_options(payload, Settings.from_env(contoso_env))
    assert failure.value.status == 400 and not session.calls


def test_auth_first_even_before_malformed_contoso_transport(contoso_env):
    worker, session = requester(contoso_env, [])
    service = ChatService(Settings.from_env(contoso_env), FakeAuth(unauthenticated()), worker)
    with pytest.raises(ChatFailure) as failure:
        service.chat(b"malformed", {"content-type": "application/json"}, "request-id")
    assert failure.value.status == 401 and not session.calls


@pytest.mark.parametrize("remaining", [30000, 74999, 79999])
def test_insufficient_context_has_no_network(contoso_env, remaining):
    worker, session = requester(contoso_env, [])
    with pytest.raises(ChatFailure) as failure:
        worker.invoke("question", OPTIONS, remaining)
    assert failure.value.status == 502 and not session.calls


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 409, 429, 500, 502, 503, 504])
def test_gateway_http_safe_and_never_retries(contoso_env, status, caplog):
    worker, session = requester(contoso_env, [
        FakeResponse(token_result()), FakeResponse(status=status, raw=b"RAW-SECRET")])
    with pytest.raises(ChatFailure) as error:
        worker.invoke("question", OPTIONS, 90000)
    assert error.value.status == (429 if status == 429 else 502)
    assert len(session.calls) == 2
    assert worker._token is None
    assert "RAW-SECRET" not in str(error.value) + error.value.message + caplog.text


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_token_http_failure_never_calls_gateway(contoso_env, status):
    worker, session = requester(contoso_env, [FakeResponse(status=status, raw=b"RAW-SECRET")])
    with pytest.raises(ChatFailure) as error:
        worker.invoke("question", OPTIONS, 90000)
    assert error.value.status == 502 and len(session.calls) == 1


@pytest.mark.parametrize("changes", [
    {"access_token": ""}, {"access_token": "bad token"}, {"access_token": "x" * 16385},
    {"token_type": "other"}, {"expires_in": "3600"}, {"expires_in": True},
    {"expires_in": 60}, {"expires_in": 86401},
])
def test_token_shape_and_expiry_validation(contoso_env, changes):
    worker, session = requester(contoso_env, [FakeResponse(token_result(**changes))])
    with pytest.raises(ChatFailure):
        worker.invoke("question", OPTIONS, 90000)
    assert len(session.calls) == 1 and worker._token is None


def test_single_ram_cache_and_expiry_margin(contoso_env):
    worker, session = requester(contoso_env, [FakeResponse(token_result()), FakeResponse(runtime_result()),
        FakeResponse(runtime_result()), FakeResponse(token_result()), FakeResponse(runtime_result())])
    before = time.monotonic()
    worker.invoke("first", OPTIONS, 90000)
    assert before + 3539 < worker._expires < time.monotonic() + 3541
    worker.invoke("second", OPTIONS, 90000)
    assert len(session.calls) == 3
    worker._expires = 0
    worker.invoke("third", OPTIONS, 90000)
    assert len(session.calls) == 5
    assert not hasattr(worker, "cache_file")


@pytest.mark.parametrize("changes", [
    {"status": "unavailable"}, {"answer": ""}, {"answer": "\ud800"},
    {"provider_id": None}, {"studio_variant": "copilot-harness"}, {"sources": []},
    {"citations": []}, {"execution_path": []}, {"execution_path": [None]},
    {"caller_id": "private"}, {"sources": [{"source_id": "source-1"}]},
    {"citations": [{"source_id": "invented", "version": "v1", "section": "x"}]},
    {"citations": [{"source_id": "source-1", "version": "wrong", "section": "x"}]},
])
def test_malformed_runtime_rejected_without_generated_answer(contoso_env, changes):
    value = {**runtime_result(), **changes}
    worker, _ = requester(contoso_env, [FakeResponse(token_result()), FakeResponse(value)])
    with pytest.raises(ChatFailure) as error:
        worker.invoke("q", OPTIONS, 90000)
    assert error.value.status == 502


def test_attribution_provider_mismatch_and_duplicate_sources():
    for duplicate in (False, True):
        value = runtime_result()
        if duplicate:
            value["sources"].append(copy.deepcopy(value["sources"][0]))
        else:
            value["sources"][0]["provider_id"] = "wrong"
        with pytest.raises(ChatFailure):
            validate_result(value, OPTIONS)


def test_unknown_no_sources_and_insufficient_evidence_preserve_literal():
    value = runtime_result(status="no-matching-capability")
    value.update(provider_id=None, studio_variant=None, sources=[], citations=[],
                 answer="No approved agent matches this request. No knowledge provider was called.",
                 execution_path=["Verified gateway", "No matching capability"])
    result = validate_result(value, OPTIONS)
    assert result["answer"] == value["answer"] and not result["contoso"]["sources"]
    value["provider_id"] = "pretend-provider"
    with pytest.raises(ChatFailure):
        validate_result(value, OPTIONS)
    value = runtime_result(status="insufficient-evidence")
    value.update(sources=[], citations=[], answer="The source does not provide this percentage.")
    assert validate_result(value, OPTIONS)["answer"] == value["answer"]
    value = runtime_result(variant=None)
    assert validate_result(value, OPTIONS)["contoso"]["studio_variant"] is None


def test_output_bound_sets_required_flag():
    value = runtime_result()
    value["answer"] = "😀" * 4001
    result = validate_result(value, OPTIONS)
    assert result["answer"] == "😀" * 4000 and result["output_limited"] is True


@pytest.mark.parametrize("response", [
    FakeResponse(raw=b"x" * (MAX_DOCUMENT_BYTES + 1)),
    FakeResponse(runtime_result(), headers={"Content-Length": str(MAX_DOCUMENT_BYTES + 1)}),
    FakeResponse(runtime_result(), headers={"Content-Type": "text/plain"}),
    FakeResponse(runtime_result(), headers={"Content-Encoding": "gzip"}),
    FakeResponse(raw=b'{"status":"answered","status":"answered"}'),
    FakeResponse(raw=b'{"value":NaN}'),
    FakeResponse(raw=b"\xff"),
    requests.Timeout("PRIVATE-TRANSPORT-ERROR"),
])
def test_transport_caps_and_safe_errors(contoso_env, response, caplog):
    worker, _ = requester(contoso_env, [FakeResponse(token_result()), response])
    with pytest.raises(ChatFailure) as error:
        worker.invoke("q", OPTIONS, 90000)
    assert error.value.status == 502
    assert "PRIVATE-TRANSPORT-ERROR" not in str(error.value) + caplog.text


def test_overall_worker_deadline_and_bounded_concurrent_slots(contoso_env, monkeypatch):
    import contoso_requester
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    worker, session = requester(contoso_env, [])
    monkeypatch.setattr(contoso_requester, "CONTOSO_DEADLINE_SECONDS", 0.03)

    def blocked(*_):
        started.set()
        release.wait(2)
        finished.set()
        raise requests.Timeout("private")

    monkeypatch.setattr(worker, "_invoke", blocked)
    begin = time.monotonic()
    try:
        with pytest.raises(ChatFailure):
            worker.invoke("q", OPTIONS, 90000)
        assert time.monotonic() - begin < 0.3 and started.is_set()
        other, _ = requester(contoso_env, [])
        with pytest.raises(ChatFailure):
            other.invoke("q", OPTIONS, 90000)
        assert not session.calls
    finally:
        release.set()
        assert finished.wait(1)
        # Wait until the transport's finally releases its one global slot.
        for _ in range(100):
            if contoso_requester._transport_slot.acquire(blocking=False):
                contoso_requester._transport_slot.release()
                break
            time.sleep(0.001)
