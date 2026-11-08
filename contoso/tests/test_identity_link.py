import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from contoso.identity_link import IdentityLinkProbe
from contoso.router.core import AccessDenied
from contoso.providers import TransportError


@pytest.fixture
def anyio_contoso_backend():
    return "asyncio"


class Context:
    user_id = "actual-gateway-platform-key"

    def platform_headers(self):
        return {"x-agent-foundry-call-id": "fresh-call"}


class Credential:
    def __init__(self):
        self.calls = []

    def get_token(self, scope):
        self.calls.append(scope)
        return SimpleNamespace(token="own-service-token", expires_on=time.time() + 300)


class Transport:
    def __init__(self, valid=True):
        self.calls = []
        self.valid = valid

    async def invoke(self, text, token):
        self.calls.append((text, token))
        return json.dumps({
            "status": "identity-bootstrap" if self.valid else "answered",
            "platform_caller_id": "actual-router-platform-key",
            "platform_call_id": "downstream-call",
            "knowledge_access": False,
        })


def setup(*, valid=True, caller="actual-gateway-platform-key"):
    credential, transport = Credential(), Transport(valid)
    host = IdentityLinkProbe(
        credential, endpoint="https://private.services.ai.azure.com/a2a",
        allowed_callers=frozenset({caller}), context_factory=Context, transport=transport,
    )
    return host, credential, transport


@pytest.mark.anyio
async def test_link_proves_downstream_actor_using_only_own_service_identity():
    host, credential, transport = setup()
    result = await host.answer({"input": "CONTOSO identity link probe"}, asyncio.Event())
    assert credential.calls == ["https://ai.azure.com/.default"]
    assert transport.calls == [("CONTOSO identity probe", "own-service-token")]
    assert result["status"] == "identity-link-bootstrap"
    assert result["knowledge_access"] is False
    assert result["downstream"]["knowledge_access"] is False
    assert result["downstream"]["platform_caller_id"] == "actual-router-platform-key"
    assert "own-service-token" not in json.dumps(result)


@pytest.mark.anyio
@pytest.mark.parametrize("question", [
    "CONTOSO identity probe", "What is the approved intervention?",
    "CONTOSO identity link probe with another provider",
])
async def test_only_exact_link_question_can_acquire_service_token(question):
    host, credential, transport = setup()
    with pytest.raises(AccessDenied):
        await host.answer({"input": question}, asyncio.Event())
    assert not credential.calls
    assert not transport.calls


@pytest.mark.anyio
async def test_unapproved_platform_caller_cannot_spoof_input_or_acquire_token():
    host, credential, transport = setup(caller="another-platform-caller")
    with pytest.raises(AccessDenied):
        await host.answer(
            {"input": "CONTOSO identity link probe", "caller_id": "another-platform-caller"},
            asyncio.Event(),
        )
    assert not credential.calls
    assert not transport.calls


@pytest.mark.anyio
async def test_cancellation_before_link_does_not_acquire_token():
    host, credential, transport = setup()
    signal = asyncio.Event()
    signal.set()
    with pytest.raises(asyncio.CancelledError):
        await host.answer({"input": "CONTOSO identity link probe"}, signal)
    assert not credential.calls
    assert not transport.calls


@pytest.mark.anyio
async def test_no_business_reply_can_be_accepted_as_identity_proof():
    host, _, _ = setup(valid=False)
    with pytest.raises(TransportError):
        await host.answer({"input": "CONTOSO identity link probe"}, asyncio.Event())
