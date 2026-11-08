from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from contoso import gateway_probe
from contoso.publication import PublicationFailure


def setup(monkeypatch, payload, *, status="completed"):
    calls = []
    response = SimpleNamespace(
        status=status, output=[SimpleNamespace(
            type="message", content=[
                SimpleNamespace(type="output_text", text=json.dumps(payload)),
            ],
        )],
    )

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    def openai(**kwargs):
        calls.append(kwargs)
        return nullcontext(client)

    project = SimpleNamespace(get_openai_client=openai)
    monkeypatch.setattr(gateway_probe, "ManagedIdentityCredential", lambda: nullcontext("own-mi"))
    monkeypatch.setattr(
        gateway_probe, "AIProjectClient",
        lambda **kwargs: nullcontext(project),
    )
    return calls


def payload():
    return {
        "status": "identity-bootstrap", "platform_caller_id": "actual-opaque-key",
        "platform_call_id": "actual-platform-call", "knowledge_access": False,
    }


def test_fixed_sdk_question_own_service_identity_and_no_response_store(monkeypatch):
    calls = setup(monkeypatch, payload())
    config = SimpleNamespace(project_endpoint="https://private.example")
    result = gateway_probe.probe_service(config, "contoso-foundry-sdk-agent")
    assert calls == [
        {"agent_name": "contoso-foundry-sdk-agent"},
        {"input": "CONTOSO identity probe", "store": False, "timeout": 60},
    ]
    assert result == {"service": "contoso-foundry-sdk-agent", "integration": "sdk", **payload()}


@pytest.mark.parametrize("change,status", [
    ({"knowledge_access": True}, "completed"),
    ({"status": "answered"}, "completed"),
    ({"platform_caller_id": ""}, "completed"),
    ({"platform_call_id": "\n"}, "completed"),
    ({}, "failed"),
])
def test_no_business_result_or_invalid_identity_is_accepted(monkeypatch, change, status):
    setup(monkeypatch, {**payload(), **change}, status=status)
    with pytest.raises(PublicationFailure, match="identity_probe_unavailable"):
        gateway_probe.probe_service(
            SimpleNamespace(project_endpoint="https://private.example"), "contoso-knowledge-agent",
        )


@pytest.mark.parametrize("service", ["other-agent", None, []])
def test_no_arbitrary_agent_invocation(service):
    with pytest.raises(ValueError):
        gateway_probe.probe_service(SimpleNamespace(), service)
