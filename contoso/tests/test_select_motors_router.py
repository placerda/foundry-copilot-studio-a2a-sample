from copy import deepcopy
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

from azure.ai.projects.models import AgentVersionDetails
from azure.core.exceptions import ServiceRequestError
import pytest

from contoso.ops import select_motors_router as select


@pytest.fixture(autouse=True)
def forbid_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No network or credential access is allowed")

    for name in ("AzureCliCredential", "AIProjectClient"):
        monkeypatch.setattr(select, name, forbidden)
    monkeypatch.setattr(select.requests, "Session", forbidden)


@pytest.fixture
def operator(monkeypatch):
    active = {
        "kind": "hosted", "cpu": "0.25", "memory": "0.5Gi",
        "container_configuration": {
            "image": "contosotest.azurecr.io/contoso-runtime@sha256:" + "a" * 64,
        },
        "environment_variables": {
            "CONTOSO_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
            "CONTOSO_PLATFORM_CALLER_IDS": '["original-python-caller"]',
            "CONTOSO_TEAMS_PLATFORM_CALLER_IDS": '["unbound:teams"]',
            "PRIVATE_SETTING": "must-not-appear-in-output",
        },
        "protocol_versions": [
            {"protocol": protocol, "version": "1.0"}
            for protocol in ("a2a", "activity", "invocations", "responses")
        ],
    }
    target = deepcopy(active)
    target["environment_variables"]["CONTOSO_TEAMS_PLATFORM_CALLER_IDS"] = '["reviewed-teams-id"]'
    versions = {
        version: AgentVersionDetails({"name": select.ROUTER, "version": version,
                                      "definition": definition})
        for version, definition in (("14", active), ("15", target))
    }
    bindings = select.Bindings(
        schema_version=1,
        tenant_id="11111111-1111-4111-8111-111111111111",
        operator_object_id="22222222-2222-4222-8222-222222222222",
        project_resource_id=(
            "/subscriptions/33333333-3333-4333-8333-333333333333/"
            "resourceGroups/rg-contoso-test/providers/Microsoft.CognitiveServices/"
            "accounts/contoso-test/projects/contoso-project"
        ),
        project_endpoint="https://contoso-test.services.ai.azure.com/api/projects/contoso-project",
        router_version="14", router_definition_sha256=select.digest(active),
        router_source_sha256="b" * 64,
        router_principal_id="44444444-4444-4444-8444-444444444444",
        logistics_version="5", logistics_definition_sha256="c" * 64,
        logistics_principal_id="55555555-5555-4555-8555-555555555555",
        logistics_model="reviewed-model", studio_source_sha256="d" * 64,
    )
    parent = {
        "name": select.ROUTER, "state": "enabled",
        "versions": {"latest": {"version": "15"}},
        "instance_identity": {"principal_id": bindings.router_principal_id},
        "agent_endpoint": {
            "version_selector": select._selector("14"),
            "protocols": ["a2a", "activity", "invocations", "responses"],
            "authorization_schemes": [
                {"type": "Entra", "isolation_key_source": {"kind": "Entra"}},
                {"type": "BotServiceRbac"},
            ],
            "protocol_configuration": {"activity": {"enable_m365_public_endpoint": True}},
            "publish_approval_status": "not_published",
        },
        "m365_registration": {"scope": "Shared", "id": "preserved-registration"},
        "agent_card": {"description": "preserved-card"},
        "future_field": {"preserved": True},
    }
    client = Mock()
    client.agents.get.side_effect = lambda name: SimpleNamespace(as_dict=lambda: deepcopy(parent))
    client.agents.get_version.side_effect = lambda name, version: versions[version]

    def update(name, *, body):
        parent["agent_endpoint"]["version_selector"] = deepcopy(
            body["agent_endpoint"]["version_selector"],
        )
        return SimpleNamespace(as_dict=lambda: deepcopy(parent))

    client.agents.update_details.side_effect = update
    administrator = select.MotorsAdministrator(bindings, Mock(), Mock(), client)
    administrator.run = Mock()
    postflight = Mock()
    monkeypatch.setattr(select, "MotorsAdministrator", Mock(return_value=postflight))
    return administrator, parent, versions, postflight


def run(operator, **kwargs):
    administrator, parent, versions, _ = operator
    target = kwargs.pop("target_version", "15")
    target_hash = (
        kwargs.pop("target_hash") if "target_hash" in kwargs
        else select.digest(versions[target].definition.as_dict())
    )
    expected = select.digest(select._parent_invariants(parent))
    return select.select_version(
        administrator, target, target_hash,
        expected_parent_sha256=kwargs.pop("expected_parent_sha256", expected),
        **kwargs,
    )


def test_preview_is_live_read_only_with_explicit_reverse_plan(operator):
    administrator, parent, _, _ = operator
    before = deepcopy(parent)
    result = run(operator)
    assert result["status"] == "verified-selection-preview"
    assert result["live_state_verified"] is True
    assert result["selector_mutation_performed"] is False
    assert result["service_compare_and_swap_proven"] is False
    assert result["rollback"]["from_version"] == "15"
    assert result["rollback"]["target_version"] == "14"
    assert result["rollback"]["target_definition_sha256"] == administrator.bindings.router_definition_sha256
    assert parent == before
    administrator.client.agents.update_details.assert_not_called()
    assert administrator.run.call_args_list == [call("preflight"), call("preflight")]
    assert "must-not-appear-in-output" not in json.dumps(result)


def test_apply_patches_only_selector_and_preserves_every_other_parent_field(operator):
    administrator, parent, _, postflight = operator
    before = deepcopy(parent)
    result = run(operator, apply=True, exclusive_change_window=True)
    expected = deepcopy(before)
    expected["agent_endpoint"]["version_selector"] = select._selector("15")
    assert parent == expected
    assert result["status"] == "selected-and-verified"
    assert result["selector_mutation_performed"] is True
    administrator.client.agents.update_details.assert_called_once_with(
        select.ROUTER, body={"agent_endpoint": {"version_selector": select._selector("15")}},
    )
    postflight.run.assert_called_once_with("preflight")


def test_explicit_rollback_uses_reverse_pins_and_preserves_native_teams(operator):
    administrator, parent, _, _ = operator
    before = deepcopy(parent)
    forward = run(operator, apply=True, exclusive_change_window=True)
    rollback = forward["rollback"]
    administrator.bindings = replace(
        administrator.bindings, router_version=rollback["from_version"],
        router_definition_sha256=rollback["from_definition_sha256"],
    )
    result = run(
        operator, target_version=rollback["target_version"],
        target_hash=rollback["target_definition_sha256"],
        expected_parent_sha256=rollback["expected_parent_sha256"],
        apply=True, exclusive_change_window=True,
    )
    assert result["status"] == "selected-and-verified"
    assert parent == before
    assert administrator.client.agents.update_details.call_count == 2
    assert parent["agent_endpoint"]["protocol_configuration"]["activity"][
        "enable_m365_public_endpoint"
    ] is True


def test_already_selected_is_verified_without_patch_even_with_apply(operator):
    administrator, _, _, _ = operator
    result = run(operator, target_version="14", apply=True)
    assert result["status"] == "verified-already-selected"
    assert result["selector_mutation_performed"] is False
    administrator.client.agents.update_details.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"target_version": "@latest", "target_hash": "a" * 64},
    {"target_version": "../15", "target_hash": "a" * 64},
    {"target_hash": "short"},
    {"expected_parent_sha256": "short"},
    {"apply": True, "expected_parent_sha256": None, "exclusive_change_window": True},
    {"apply": True},
])
def test_invalid_or_unfenced_selection_fails_before_network(operator, kwargs):
    with pytest.raises(ValueError):
        run(operator, **kwargs)
    operator[0].run.assert_not_called()
    operator[0].client.agents.update_details.assert_not_called()


@pytest.mark.parametrize("drift", ["parent", "definition", "python-caller", "tenant", "protocol"])
def test_target_and_reviewed_parent_drift_fail_before_mutation(operator, drift):
    administrator, _, versions, _ = operator
    kwargs = {"apply": True, "exclusive_change_window": True}
    if drift == "parent":
        kwargs["expected_parent_sha256"] = "0" * 64
    elif drift == "definition":
        kwargs["target_hash"] = "0" * 64
    else:
        target = versions["15"].definition.as_dict()
        if drift == "protocol":
            target["protocol_versions"].pop()
        else:
            key = "CONTOSO_PLATFORM_CALLER_IDS" if drift == "python-caller" else "CONTOSO_ENTRA_TENANT_ID"
            target["environment_variables"][key] = "unreviewed-admission"
        versions["15"] = AgentVersionDetails({
            "name": select.ROUTER, "version": "15", "definition": target,
        })
    with pytest.raises(select.SelectionFailure):
        run(operator, **kwargs)
    administrator.client.agents.update_details.assert_not_called()


@pytest.mark.parametrize("protocols", [None, []])
def test_missing_protocol_contract_is_rejected_even_when_both_definitions_match(operator, protocols):
    administrator, _, versions, _ = operator
    for version in versions:
        definition = versions[version].definition.as_dict()
        definition["protocol_versions"] = protocols
        versions[version] = AgentVersionDetails({
            "name": select.ROUTER, "version": version, "definition": definition,
        })
    administrator.bindings = replace(
        administrator.bindings,
        router_definition_sha256=select.digest(versions["14"].definition.as_dict()),
    )
    with pytest.raises(select.SelectionFailure, match="protocol_or_environment_contract_drift"):
        run(operator, apply=True, exclusive_change_window=True)
    administrator.client.agents.update_details.assert_not_called()


def test_drift_between_preflights_is_rejected_without_overwriting_concurrent_writer(operator):
    administrator, parent, _, _ = operator

    def preflight(*args):
        if administrator.run.call_count == 2:
            parent["agent_endpoint"]["authorization_schemes"].append({"type": "unreviewed"})

    administrator.run.side_effect = preflight
    with pytest.raises(select.SelectionFailure, match="parent_drift"):
        run(operator, apply=True, exclusive_change_window=True)
    administrator.client.agents.update_details.assert_not_called()


@pytest.mark.parametrize("failure", ["transport", "readback", "postflight"])
def test_uncertain_write_or_verification_never_retries_or_automatically_restores(operator, failure):
    administrator, parent, _, postflight = operator
    original = administrator.client.agents.update_details.side_effect

    def update(*args, **kwargs):
        original(*args, **kwargs)
        if failure == "transport":
            raise ServiceRequestError("sensitive transport details must not be printed")
        if failure == "readback":
            parent["agent_endpoint"]["version_selector"] = select._selector("16")

    administrator.client.agents.update_details.side_effect = update
    if failure == "postflight":
        postflight.run.side_effect = select.AdministrationFailure("postflight drift")
    with pytest.raises(select.SelectionFailure, match="outcome_unconfirmed") as caught:
        run(operator, apply=True, exclusive_change_window=True)
    assert caught.value.mutation_attempted is True
    administrator.client.agents.update_details.assert_called_once()
    assert parent["agent_endpoint"]["version_selector"] != select._selector("14")


@pytest.mark.parametrize("apply,cleanup_failure", [
    (False, False), (False, True), (True, True),
])
def test_cli_transport_and_cleanup_failures_preserve_mutation_knowledge(
    operator, monkeypatch, tmp_path, capsys, apply, cleanup_failure,
):
    administrator, parent, versions, _ = operator
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(asdict(administrator.bindings)), encoding="utf-8")
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    credential = Mock()
    credential.__enter__ = Mock(return_value=credential)
    credential.__exit__ = Mock(return_value=False)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    client = Mock()
    client.__enter__ = Mock(return_value=administrator.client)
    client.__exit__ = Mock(return_value=False)
    if cleanup_failure:
        client.__exit__.side_effect = ServiceRequestError("sensitive cleanup details")
    factory = Mock(return_value=client)
    monkeypatch.setattr(select, "AzureCliCredential", Mock(return_value=credential))
    monkeypatch.setattr(select.requests, "Session", Mock(return_value=session))
    monkeypatch.setattr(select, "AIProjectClient", factory)
    monkeypatch.setattr(select, "MotorsAdministrator", Mock(return_value=administrator))
    target = "15" if apply else "14"
    arguments = [
        "--bindings", str(path), "--target-version", target,
        "--target-definition-sha256", select.digest(versions[target].definition.as_dict()),
    ]
    if apply:
        arguments.extend([
            "--apply", "--exclusive-change-window",
            "--expected-parent-sha256", select.digest(select._parent_invariants(parent)),
        ])
    assert select.main(arguments) == (1 if cleanup_failure else 0)
    captured = capsys.readouterr()
    assert "sensitive cleanup details" not in captured.err
    if cleanup_failure:
        result = json.loads(captured.err)
        assert captured.out == ""
        assert result["status"] == "failed"
        assert result["selector_mutation_attempted"] is apply
        assert result["independent_review_required_before_retry"] is apply
    else:
        result = json.loads(captured.out)
        assert result["status"] == "verified-already-selected"
    assert session.trust_env is False
    assert factory.call_args.kwargs["retry_total"] == 0
    assert factory.call_args.kwargs["connection_timeout"] == 5
    assert factory.call_args.kwargs["read_timeout"] == 30
    assert administrator.client.agents.update_details.call_count == int(apply)
