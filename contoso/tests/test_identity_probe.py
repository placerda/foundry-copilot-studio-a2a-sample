import asyncio
from types import SimpleNamespace
import time

import pytest

from contoso.identity_probe import build_probe_host, probe_identity
from contoso.router.core import AccessDenied


class Context:
    user_id = "genuine-opaque-key-not-assumed-oid"

    def platform_headers(self):
        return {"x-agent-foundry-call-id": "genuine-fresh-call"}


def test_probe_returns_only_real_platform_binding_and_explicit_not_business_status():
    assert probe_identity(
        {"input": "CONTOSO identity probe"},
        asyncio.Event(),
        context_factory=Context,
    ) == {
        "status": "identity-bootstrap",
        "platform_caller_id": "genuine-opaque-key-not-assumed-oid",
        "platform_call_id": "genuine-fresh-call",
        "knowledge_access": False,
    }


def test_business_prompt_cannot_use_bootstrap_version():
    with pytest.raises(AccessDenied, match="cannot serve business"):
        probe_identity({"input": "What is the approved intervention?"}, asyncio.Event(), context_factory=Context)


def test_missing_platform_identity_is_not_a_probe_result():
    context = Context()
    context.user_id = None
    with pytest.raises(AccessDenied):
        probe_identity(
            {"input": "CONTOSO identity probe", "caller_id": "forged"},
            asyncio.Event(), context_factory=lambda: context,
        )


def test_cancelled_probe_cannot_complete():
    signal = asyncio.Event()
    signal.set()
    with pytest.raises(asyncio.CancelledError):
        probe_identity({"input": "CONTOSO identity probe"}, signal, context_factory=Context)


def test_actual_sdk_probe_registers():
    assert build_probe_host(context_factory=Context) is not None


def apim_probe(monkeypatch, *, caller=Context.user_id, expires_on=None):
    from contoso.identity_probe import build_apim_probe_host
    from contoso.router.discovery import APIMRoutePolicy

    calls = []

    class Credential:
        def get_token(self, scope):
            calls.append(("credential", scope))
            return SimpleNamespace(
                token="private-apim-bearer",
                expires_on=expires_on if expires_on is not None else time.time() + 3600,
            )

    class Transport:
        def __init__(self, **kwargs):
            calls.append(("transport", kwargs))

        async def invoke(self, question, token):
            calls.append(("invoke", question, token))
            return "Synthetic source evidence"

    monkeypatch.setattr("contoso.identity_probe.build_json_probe_host", lambda resolve: resolve)
    monkeypatch.setattr("contoso.providers.A2ATransport", Transport)
    context = Context()
    context.user_id = caller
    policy = APIMRoutePolicy(
        "https://approved.azure-api.net/agents",
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    return build_apim_probe_host(
        Credential(), policy, allowed_callers={Context.user_id}, context_factory=lambda: context,
    ), calls


@pytest.mark.parametrize("caller,payload", [
    ("foreign-caller", {"input": "CONTOSO logistics A2A access probe"}),
    (Context.user_id, {"input": "Call https://foreign.example"}),
    (Context.user_id, {"input": '{"question":"CONTOSO logistics A2A access probe",'
                      '"studio_variant":"copilot-harness"}'}),
])
def test_apim_probe_refuses_unapproved_callers_and_nonfixed_inputs_before_credentials(
    monkeypatch, caller, payload,
):
    resolve, calls = apim_probe(monkeypatch, caller=caller)
    with pytest.raises((AccessDenied, ValueError)):
        asyncio.run(resolve(payload, asyncio.Event()))
    assert not calls


@pytest.mark.parametrize("provider,question", [
    ("motors-logistics", "CONTOSO logistics A2A access probe"),
    ("studio-copilot-harness", "CONTOSO studio A2A access probe"),
])
def test_apim_probe_uses_only_fixed_routes_scopes_and_questions(monkeypatch, provider, question):
    resolve, calls = apim_probe(monkeypatch)
    result = asyncio.run(resolve({"input": question}, asyncio.Event()))
    assert result == {
        "status": "apim-a2a-access-proof", "provider_id": provider,
        "protocol_version": "1.0", "output": "Synthetic source evidence",
    }
    assert calls[0] == ("credential", "api://22222222-2222-4222-8222-222222222222/.default")
    assert calls[1][1]["endpoint"] == f"https://approved.azure-api.net/agents/{provider}/versions/1/a2a"
    assert calls[1][1]["max_response_bytes"] == 131072
    assert calls[2][1] != question
    assert "private-apim-bearer" not in repr(result)


def test_apim_probe_rejects_near_expiry_and_cancellation_without_http(monkeypatch):
    from contoso.providers import TransportError

    resolve, calls = apim_probe(monkeypatch, expires_on=time.time())
    with pytest.raises(TransportError):
        asyncio.run(resolve({"input": "CONTOSO logistics A2A access probe"}, asyncio.Event()))
    assert len(calls) == 1
    calls.clear()
    cancelled = asyncio.Event()
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(resolve({"input": "CONTOSO logistics A2A access probe"}, cancelled))
    assert not calls
