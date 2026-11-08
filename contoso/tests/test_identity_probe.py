import asyncio

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
