import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from contoso.router.core import AccessDenied, CatalogError, STUDIOVariant
from contoso.router.hosted import HostedRouter, parse_input
from contoso.router.runtime import RuntimeResult
from contoso.providers import ProviderTimeout


def context(caller="platform-opaque-caller", headers=None):
    return SimpleNamespace(
        user_id=caller,
        platform_headers=lambda: headers if headers is not None else {
            "x-agent-foundry-call-id": "opaque-call",
        },
    )


@pytest.fixture
def runtime():
    return SimpleNamespace(
        catalog=SimpleNamespace(require_caller=Mock()),
        run=AsyncMock(return_value=RuntimeResult("no-matching-capability", "No match", None, ())),
    )


def test_uses_sdk_context_not_prompt_identity(runtime):
    adapter = HostedRouter(runtime, context_factory=lambda: context())
    request = {"input": json.dumps({"question": "Payroll?", "studio_variant": "standard"}),
               "user": "prompt-claimed-caller", "metadata": {"caller_id": "another-caller"}}
    result = asyncio.run(adapter.answer(request, asyncio.Event()))
    assert result.status == "no-matching-capability"
    runtime.catalog.require_caller.assert_called_once_with("platform-opaque-caller")
    runtime.run.assert_awaited_once_with(
        "Payroll?", caller_id="platform-opaque-caller", variant="standard",
    )


@pytest.mark.parametrize("platform", [
    context(None), context(""), context("bad\x00caller"),
    context(headers={}), context(headers={"x-agent-foundry-call-id": ""}),
    context(headers={"x-agent-foundry-call-id": "bad\ncall"}),
    context(headers={"x-agent-user-id": "forged", "x-agent-foundry-call-id": "call"}),
])
def test_missing_or_invalid_platform_context_never_invokes(runtime, platform):
    adapter = HostedRouter(runtime, context_factory=lambda: platform)
    with pytest.raises(AccessDenied):
        asyncio.run(adapter.answer({"input": "Question"}, asyncio.Event()))
    runtime.run.assert_not_awaited()


def test_unknown_platform_caller_is_rejected_before_input_parsing(runtime):
    runtime.catalog.require_caller.side_effect = AccessDenied("Not shared")
    adapter = HostedRouter(runtime, context_factory=lambda: context("unknown-platform-caller"))
    with pytest.raises(AccessDenied):
        asyncio.run(adapter.answer({"input": None}, asyncio.Event()))
    runtime.run.assert_not_awaited()


def test_context_factory_is_read_again_for_each_request(runtime):
    factory = Mock(side_effect=[context("first"), context("second")])
    adapter = HostedRouter(runtime, context_factory=factory)
    for _ in range(2):
        asyncio.run(adapter.answer({"input": "Question"}, asyncio.Event()))
    assert [call.kwargs["caller_id"] for call in runtime.run.await_args_list] == ["first", "second"]


@pytest.mark.parametrize("payload", [
    {"input": "Question"},
    {"input": [{"role": "user", "content": "Question"}]},
    {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Question"}]}]},
])
def test_supported_stateless_message_shapes(payload):
    assert parse_input(payload) == ("Question", None)


@pytest.mark.parametrize("payload", [
    None, [],
    {"input": None}, {"input": []},
    {"input": [{"role": "system", "content": "Question"}]},
    {"input": [{"role": "user", "content": [{"type": "image", "text": "Question"}]}]},
    {"input": "Question", "previous_response_id": "other-user-response"},
    {"input": "Question", "conversation": "other-user-conversation"},
    {"input": "Question", "store": True},
    {"input": "Question", "background": True},
    {"input": '{"question":"Question","studio_variant":"standard","caller_id":"forged"}'},
    {"input": '{"question":"One","question":"Two","studio_variant":"standard"}'},
    {"input": '{"question":123,"studio_variant":"standard"}'},
    {"input": '{"question":"Question","studio_variant":[]}'},
    {"input": "{"},
])
def test_unsupported_request_shapes_fail_closed(payload):
    with pytest.raises(CatalogError):
        parse_input(payload)


def test_pre_cancelled_request_does_no_provider_work(runtime):
    cancellation = asyncio.Event()
    cancellation.set()
    adapter = HostedRouter(runtime, context_factory=lambda: context())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.answer({"input": "Question"}, cancellation))
    runtime.run.assert_not_awaited()


def test_cancellation_stops_in_progress_work(runtime):
    async def run():
        cancelled_work = asyncio.Event()
        async def pending(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_work.set()
        runtime.run.side_effect = pending
        cancellation = asyncio.Event()
        adapter = HostedRouter(runtime, context_factory=lambda: context())
        work = asyncio.create_task(adapter.answer({"input": "Question"}, cancellation))
        await asyncio.sleep(0)
        cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert cancelled_work.is_set()
    asyncio.run(run())


def test_timeout_is_an_error_not_a_completed_answer(runtime):
    async def pending(*args, **kwargs):
        await asyncio.Event().wait()
    runtime.run.side_effect = pending
    adapter = HostedRouter(runtime, context_factory=lambda: context(), overall_timeout=0.01)
    with pytest.raises(ProviderTimeout):
        asyncio.run(adapter.answer({"input": "Question"}, asyncio.Event()))


def test_provider_error_is_not_rewritten_as_text(runtime):
    runtime.run.side_effect = ProviderTimeout("Provider unavailable")
    adapter = HostedRouter(runtime, context_factory=lambda: context())
    with pytest.raises(ProviderTimeout, match="Provider unavailable"):
        asyncio.run(adapter.answer({"input": "Question"}, asyncio.Event()))


def test_harness_only_host_selects_configured_variant_from_plain_question(runtime):
    adapter = HostedRouter(
        runtime, context_factory=lambda: context(), fixed_variant=STUDIOVariant.COPILOT_HARNESS,
    )
    asyncio.run(adapter.answer({"input": "Question"}, asyncio.Event()))
    runtime.run.assert_awaited_once_with(
        "Question", caller_id="platform-opaque-caller", variant="copilot-harness",
    )


def test_harness_only_host_rejects_prompt_selected_standard_without_provider_work(runtime):
    adapter = HostedRouter(
        runtime, context_factory=lambda: context(), fixed_variant=STUDIOVariant.COPILOT_HARNESS,
    )
    with pytest.raises(CatalogError, match="not available"):
        asyncio.run(adapter.answer(
            {"input": '{"question":"Question","studio_variant":"standard"}'}, asyncio.Event(),
        ))
    runtime.run.assert_not_awaited()


def test_fixed_variant_requires_typed_admin_configuration(runtime):
    with pytest.raises(CatalogError):
        HostedRouter(runtime, fixed_variant="copilot-harness")
