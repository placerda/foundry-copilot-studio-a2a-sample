from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

from azure.ai.projects.models import AgentVersionDetails
from azure.core.exceptions import HttpResponseError, ServiceRequestError
import pytest

from contoso.ops import publish_motors_router as publish
from contoso.ops.create_image import source_archive


SOURCE = b"reviewed-runtime-source"
SOURCE_HASH = hashlib.sha256(SOURCE).hexdigest()
IMAGE = "contosotest.azurecr.io/contoso-runtime@sha256:" + "f" * 64


@pytest.fixture(autouse=True)
def forbid_network_and_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No network or credential access is allowed")

    for name in ("AzureCliCredential", "AIProjectClient"):
        monkeypatch.setattr(publish, name, forbidden)
    monkeypatch.setattr(publish.requests, "Session", forbidden)
    monkeypatch.setattr(publish, "source_archive", Mock(return_value=SOURCE))


@pytest.fixture
def bindings():
    return publish.Bindings(
        schema_version=1,
        tenant_id="11111111-1111-4111-8111-111111111111",
        operator_object_id="22222222-2222-4222-8222-222222222222",
        project_resource_id=(
            "/subscriptions/33333333-3333-4333-8333-333333333333/"
            "resourceGroups/rg-contoso-test/providers/Microsoft.CognitiveServices/"
            "accounts/contoso-test/projects/contoso-project"
        ),
        project_endpoint="https://contoso-test.services.ai.azure.com/api/projects/contoso-project",
        router_version="14", router_definition_sha256="a" * 64,
        router_source_sha256="b" * 64,
        router_principal_id="44444444-4444-4444-8444-444444444444",
        logistics_version="5", logistics_definition_sha256="c" * 64,
        logistics_principal_id="55555555-5555-4555-8555-555555555555",
        logistics_model="reviewed-model", studio_source_sha256="d" * 64,
    )


@pytest.fixture
def evidence(bindings):
    scope = bindings.project_resource_id.split("/providers/", 1)[0]
    return {
        "task_id": scope + "/providers/Microsoft.ContainerRegistry/registries/"
        "contosotest/tasks/contoso-image-builder",
        "task_principal_id": "66666666-6666-4666-8666-666666666666",
        "run_id": "reviewed-run-1", "status": "Succeeded",
        "source_sha256": SOURCE_HASH, "image": IMAGE,
    }


@pytest.fixture
def files(bindings, evidence, tmp_path):
    bindings_path, evidence_path = tmp_path / "bindings.json", tmp_path / "build.json"
    bindings_path.write_text(json.dumps(asdict(bindings)), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return bindings_path, evidence_path


def arguments(files, *, apply=False):
    bindings_path, evidence_path = files
    args = [
        "--bindings", str(bindings_path), "--image-evidence", str(evidence_path),
        "--expected-source-sha256", SOURCE_HASH, "--expected-image", IMAGE,
    ]
    return args + (["--apply"] if apply else [])


def test_offline_preview_checks_actual_archive_without_profile_or_credentials(
    files, monkeypatch, capsys,
):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    assert publish.main(arguments(files)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "offline-preview"
    assert result["source_sha256"] == SOURCE_HASH
    assert result["live_state_verified"] is result["candidate_created"] is False
    assert "NOT performed" in result["limits"]
    publish.source_archive.assert_called_once_with(publish.ROOT)


@pytest.mark.parametrize("change", [
    {"status": "Running"}, {"source_sha256": "e" * 64},
    {"image": IMAGE.replace("f" * 64, "e" * 64)},
    {"image": "https://untrusted.invalid/image"},
    {"task_principal_id": "not-a-uuid"}, {"run_id": "../run"},
    {"run_id": "run?api-version=other"}, {"run_id": 1},
    {"extra": "unreviewed"}, {"task_id": "https://untrusted.invalid/task"},
    {"task_id": "/subscriptions/../../outside"},
])
def test_unreviewed_evidence_fails_offline_before_archive_read(bindings, evidence, files, change):
    path = files[1]
    path.write_text(json.dumps(evidence | change), encoding="utf-8")
    with pytest.raises(ValueError):
        publish.review_build(bindings, path, SOURCE_HASH, IMAGE)
    publish.source_archive.assert_not_called()


@pytest.mark.parametrize("replacement", [
    ("rg-contoso-test", "rg-contoso-other"),
    ("33333333-3333-4333-8333-333333333333", "77777777-7777-4777-8777-777777777777"),
    ("registries/contosotest", "registries/contosoother"),
    ("tasks/contoso-image-builder", "tasks/unreviewed-builder"),
])
def test_build_registry_and_task_are_isolated_to_selected_project(
    bindings, evidence, files, replacement,
):
    evidence["task_id"] = evidence["task_id"].replace(*replacement)
    files[1].write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError):
        publish.review_build(bindings, files[1], SOURCE_HASH, IMAGE)


@pytest.mark.parametrize("source,image", [
    ("E" * 64, IMAGE), ("short", IMAGE), (SOURCE_HASH, IMAGE.replace("@sha256:", ":")),
    (SOURCE_HASH, "https://" + IMAGE), (SOURCE_HASH, IMAGE + "?token=private"),
])
def test_explicit_pins_must_be_valid(bindings, files, source, image):
    with pytest.raises(ValueError):
        publish.review_build(bindings, files[1], source, image)


def test_local_source_drift_fails_even_with_reviewed_evidence(files, monkeypatch, capsys):
    monkeypatch.setattr(publish, "source_archive", Mock(return_value=b"unreviewed change"))
    assert publish.main(arguments(files)) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["status"] == "failed"


def test_offline_preview_hashes_the_existing_runtime_archive(
    files, evidence, monkeypatch, capsys,
):
    source_hash = hashlib.sha256(source_archive(publish.ROOT)).hexdigest()
    files[1].write_text(json.dumps(evidence | {"source_sha256": source_hash}), encoding="utf-8")
    monkeypatch.setattr(publish, "source_archive", source_archive)
    args = arguments(files)
    args[args.index(SOURCE_HASH)] = source_hash
    assert publish.main(args) == 0
    assert json.loads(capsys.readouterr().out)["source_sha256"] == source_hash


def test_invalid_apply_evidence_is_rejected_before_credentials(
    files, evidence, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    files[1].write_text(json.dumps(evidence | {"status": "Failed"}), encoding="utf-8")
    assert publish.main(arguments(files, apply=True)) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)["status"] == "failed"


@pytest.mark.parametrize("content", [
    "{}", "[]", '{"status":"Succeeded","status":"Succeeded"}', "x" * 16385,
])
def test_evidence_json_is_bounded_complete_and_unambiguous(bindings, files, content):
    files[1].write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        publish.review_build(bindings, files[1], SOURCE_HASH, IMAGE)


def test_symlink_evidence_is_rejected_without_opening_it(bindings, files, monkeypatch):
    original = publish.Path.is_symlink
    monkeypatch.setattr(
        publish.Path, "is_symlink", lambda path: path == files[1] or original(path),
    )
    with pytest.raises(ValueError, match="evidence file"):
        publish.review_build(bindings, files[1], SOURCE_HASH, IMAGE)
    publish.source_archive.assert_not_called()


@pytest.fixture
def operator(bindings, evidence):
    definition = {
        "kind": "hosted", "cpu": "1", "memory": "2Gi",
        "container_configuration": {
            "image": IMAGE.replace("f" * 64, "e" * 64),
            "registry_identity": {"type": "SystemAssigned"},
        },
        "environment_variables": {
            "PYTHONUNBUFFERED": "1", "CONTOSO_TEAMS_ALLOWED_TENANTS": "reviewed-tenant",
            "CONTOSO_TEAMS_ALLOWED_CALLERS": "reviewed-caller",
            "CONTOSO_ENTRA_TENANT_ID": bindings.tenant_id,
            "CONTOSO_PLATFORM_CALLER_IDS": '["reviewed-python-caller"]',
            "CONTOSO_TEAMS_PLATFORM_CALLER_IDS": '["unbound:teams:not-observed"]',
            "PRIVATE_SETTING": "must-not-appear-in-output",
        },
        "protocol_versions": [
            {"protocol": protocol, "version": "1.0"}
            for protocol in ("a2a", "activity", "invocations", "responses")
        ],
        "telemetry_config": {"preserve": "all"}, "future_property": {"keep": [1, 2]},
    }
    parent = {
        "name": publish.ROUTER, "state": "enabled",
        "instance_identity": {"principal_id": bindings.router_principal_id},
        "versions": {"latest": {"version": bindings.router_version}},
        "agent_endpoint": {
            "url": "https://contoso-test.services.ai.azure.com/agents/router",
            "version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": bindings.router_version,
                 "traffic_percentage": 100},
            ]},
            "authorization_schemes": [
                {"type": "Entra", "isolation_key_source": {"kind": "Entra"}},
                {"type": "BotServiceRbac"},
            ],
            "protocols": ["a2a", "activity", "invocations", "responses"],
        },
        "agent_card": {"description": "reviewed-card"},
        "roles": ["existing-role"], "m365_registration": {"id": "existing-registration"},
    }
    bindings = replace(bindings, router_definition_sha256=publish.digest(definition))
    client = Mock()
    client.agents.get.return_value = SimpleNamespace(as_dict=lambda: deepcopy(parent))
    versions = {bindings.router_version: AgentVersionDetails({
        "name": publish.ROUTER, "version": bindings.router_version, "definition": definition,
    })}
    client.agents.get_version.side_effect = lambda name, number: versions[number]

    def create(name, *, definition, metadata):
        candidate = AgentVersionDetails({
            "name": name, "version": "15", "definition": definition.as_dict(),
            "metadata": deepcopy(metadata),
        })
        versions["15"] = candidate
        parent["versions"]["latest"]["version"] = "15"
        return candidate

    client.agents.create_version.side_effect = create
    registry_id = evidence["task_id"].rsplit("/tasks/", 1)[0]
    registry = {
        "id": registry_id,
        "properties": {"publicNetworkAccess": "Disabled", "loginServer": "contosotest.azurecr.io"},
    }
    run = {
        "id": registry_id + "/runs/" + evidence["run_id"],
        "properties": {
            "runId": evidence["run_id"], "task": publish.TASK_NAME, "status": "Succeeded",
            "outputImages": [{
                "repository": "contoso-runtime", "tag": SOURCE_HASH, "digest": "sha256:" + "f" * 64,
                "registry": "contosotest.azurecr.io",
            }],
        },
    }
    administrator = publish.MotorsAdministrator(bindings, Mock(), Mock(), client)
    administrator.run = Mock(return_value={"status": "verified-read-only-operation"})
    administrator._get_json = Mock(side_effect=[registry, run])
    return administrator, parent, versions, registry, run


def test_only_the_image_changes_and_candidate_is_independently_read_back(operator, evidence):
    administrator, parent, versions, _, _ = operator
    before = deepcopy(versions["14"].definition.as_dict())
    parent_before = deepcopy(parent)
    result = publish.publish_candidate(administrator, evidence)
    assert administrator.run.call_args_list == [call("preflight"), call("preflight")]
    client = administrator.client
    client.agents.create_version.assert_called_once()
    desired = deepcopy(before)
    desired["container_configuration"]["image"] = IMAGE
    assert versions["14"].definition.as_dict() == before
    assert versions["15"].definition.as_dict() == desired
    assert client.agents.create_version.call_args.kwargs["definition"].as_dict() == desired
    assert versions["15"].metadata == {
        "contoso-source-sha256": SOURCE_HASH, "contoso-base-version": "14",
        "contoso-purpose": "motors-router-image-candidate",
    }
    assert client.agents.get_version.call_args_list == [
        call(publish.ROUTER, "14"), call(publish.ROUTER, "15"), call(publish.ROUTER, "14"),
    ]
    assert publish._parent_invariants(parent) == publish._parent_invariants(parent_before)
    assert result["status"] == "created-verified-candidate"
    assert result["active_state_unchanged"] is True
    assert not any(result[key] for key in (
        "activation_performed", "business_acceptance_performed", "function_retirement_performed",
    ))
    assert "must-not-appear-in-output" not in json.dumps(result)
    assert {item[0] for item in client.agents.method_calls} == {
        "get", "get_version", "create_version",
    }


def test_current_image_is_verified_without_any_cloud_mutation(operator, evidence):
    administrator, _, versions, _, _ = operator
    versions["14"].definition.container_configuration.image = IMAGE
    administrator.bindings = replace(
        administrator.bindings,
        router_definition_sha256=publish.digest(versions["14"].definition.as_dict()),
    )
    result = publish.publish_candidate(administrator, evidence)
    assert result["status"] == "verified-already-current"
    assert result["candidate_created"] is False
    assert result["version"] == "14"
    assert {item[0] for item in administrator.client.agents.method_calls} == {"get", "get_version"}
    assert administrator.client.agents.get.call_count == 2
    assert administrator.client.agents.get_version.call_count == 2
    assert administrator._get_json.call_count == 2


def test_teams_candidate_changes_only_teams_callers_without_build_or_activation(operator):
    administrator, parent, versions, _, _ = operator
    definition, parent_before = deepcopy(versions["14"].definition.as_dict()), deepcopy(parent)
    result = publish.publish_teams_candidate(administrator, ["observed-platform-caller"])
    expected = deepcopy(definition)
    expected["environment_variables"]["CONTOSO_TEAMS_PLATFORM_CALLER_IDS"] = (
        '["observed-platform-caller"]'
    )
    assert versions["14"].definition.as_dict() == definition
    assert versions["15"].definition.as_dict() == expected
    assert versions["15"].metadata["contoso-purpose"] == "motors-router-teams-admission-candidate"
    assert versions["15"].metadata["contoso-source-sha256"] == administrator.bindings.router_source_sha256
    assert publish._parent_invariants(parent) == publish._parent_invariants(parent_before)
    assert administrator.run.call_args_list == [call("preflight"), call("preflight")]
    administrator._get_json.assert_not_called()
    assert result["status"] == "created-verified-candidate"
    assert result["teams_caller_count"] == 1
    assert result["definition_sha256"] == publish.digest(expected)
    assert "observed-platform-caller" not in json.dumps(result)
    assert {item[0] for item in administrator.client.agents.method_calls} == {
        "get", "get_version", "create_version",
    }


def test_equivalent_teams_admission_is_verified_without_a_new_version(operator):
    administrator, _, versions, _, _ = operator
    versions["14"].definition.environment_variables["CONTOSO_TEAMS_PLATFORM_CALLER_IDS"] = (
        '[ "second", "first" ]'
    )
    administrator.bindings = replace(
        administrator.bindings,
        router_definition_sha256=publish.digest(versions["14"].definition.as_dict()),
    )
    result = publish.publish_teams_candidate(administrator, ["first", "second"])
    assert result["status"] == "verified-already-current"
    assert result["candidate_created"] is False
    administrator.client.agents.create_version.assert_not_called()


@pytest.mark.parametrize("callers", [
    None, "caller", [], ["one", "one"], ["*"], ["unbound:teams:not-observed"],
    [""], [" "], ["unsafe\ncaller"], ["x" * 8193], list(map(str, range(11))),
])
def test_invalid_teams_grants_fail_before_live_reads(operator, callers):
    administrator, _, _, _, _ = operator
    with pytest.raises(ValueError):
        publish.publish_teams_candidate(administrator, callers)
    administrator.run.assert_not_called()
    assert not administrator.client.agents.method_calls


@pytest.mark.parametrize("drift", ["tenant", "protocol", "missing_admission", "invalid_admission"])
def test_teams_configuration_requires_the_existing_native_boundary(operator, drift):
    administrator, _, versions, _, _ = operator
    definition = versions["14"].definition
    if drift == "tenant":
        definition.environment_variables["CONTOSO_ENTRA_TENANT_ID"] = "another-tenant"
    elif drift == "protocol":
        definition.protocol_versions = []
    elif drift == "missing_admission":
        del definition.environment_variables["CONTOSO_TEAMS_PLATFORM_CALLER_IDS"]
    else:
        definition.environment_variables["CONTOSO_TEAMS_PLATFORM_CALLER_IDS"] = "[]"
    administrator.bindings = replace(
        administrator.bindings, router_definition_sha256=publish.digest(definition.as_dict()),
    )
    with pytest.raises(publish.PublicationFailure):
        publish.publish_teams_candidate(administrator, ["observed-caller"])
    administrator.client.agents.create_version.assert_not_called()


def test_teams_candidate_uses_the_same_independent_readback_and_drift_guards(operator):
    administrator, parent, _, _, _ = operator
    create = administrator.client.agents.create_version.side_effect

    def drift(*args, **kwargs):
        candidate = create(*args, **kwargs)
        parent["m365_registration"] = {}
        return candidate

    administrator.client.agents.create_version.side_effect = drift
    with pytest.raises(publish.PublicationFailure, match="parent_drift"):
        publish.publish_teams_candidate(administrator, ["observed-caller"])
    assert administrator.client.agents.create_version.call_count == 1


def test_teams_preview_is_offline_and_never_echoes_the_caller(files, monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    assert publish.main([
        "--bindings", str(files[0]), "--teams-platform-caller", "observed-caller",
    ]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "offline-preview" and result["teams_caller_count"] == 1
    assert result["live_state_verified"] is result["candidate_created"] is False
    assert "observed-caller" not in output
    publish.source_archive.assert_not_called()


@pytest.mark.parametrize("drift", ["selector", "identity"])
def test_drift_between_initial_preflight_and_baseline_fails_before_creation(
    operator, evidence, drift,
):
    administrator, parent, _, _, _ = operator
    reviewed_endpoint = deepcopy(parent["agent_endpoint"])
    reviewed_identity = deepcopy(parent["instance_identity"])

    def preflight(operation):
        assert operation == "preflight"
        if administrator.run.call_count == 1:
            administrator.client.agents.get.assert_not_called()
        if (parent["agent_endpoint"] != reviewed_endpoint
                or parent["instance_identity"] != reviewed_identity):
            raise publish.AdministrationFailure("agent_binding_drift")
        return {"status": "verified-read-only-operation"}

    def baseline(name):
        assert name == publish.ROUTER
        assert administrator.run.call_count >= 1
        if drift == "selector":
            parent["agent_endpoint"]["version_selector"] = {}
        else:
            parent["instance_identity"] = {}
        return SimpleNamespace(as_dict=lambda: deepcopy(parent))

    administrator.run.side_effect = preflight
    administrator.client.agents.get.side_effect = baseline
    with pytest.raises(publish.AdministrationFailure, match="agent_binding_drift"):
        publish.publish_candidate(administrator, evidence)
    assert administrator.run.call_args_list == [call("preflight"), call("preflight")]
    administrator.client.agents.create_version.assert_not_called()
    administrator.client.agents.get_version.assert_not_called()


def test_arm_reads_only_exact_registry_and_run_paths(operator, evidence):
    administrator, _, _, registry, run = operator
    publish.verify_build(administrator, evidence)
    assert administrator._get_json.call_args_list == [
        call(f"https://management.azure.com{item['id']}?api-version={publish.ACR_API}",
             publish.ARM_SCOPE)
        for item in (registry, run)
    ]


@pytest.mark.parametrize("task", ["another-image-builder", "CONTOSO-IMAGE-BUILDER", "arm-id"])
def test_run_requires_exact_task_name_without_arm_id_fallback(operator, evidence, task):
    administrator, _, _, _, run = operator
    run["properties"]["task"] = evidence["task_id"] if task == "arm-id" else task
    with pytest.raises(publish.PublicationFailure, match="build_run_mismatch"):
        publish.publish_candidate(administrator, evidence)
    administrator.client.agents.create_version.assert_not_called()


@pytest.mark.parametrize("drift", [
    "public_registry", "registry_id", "login_server", "run_id", "run_resource",
    "run_task", "run_status", "repository", "tag", "digest", "output_registry", "duplicate_image",
])
def test_unverified_live_build_never_creates_a_candidate(operator, evidence, drift):
    administrator, _, _, registry, run = operator
    properties = run["properties"]
    image = properties["outputImages"][0]
    if drift == "public_registry":
        registry["properties"]["publicNetworkAccess"] = "Enabled"
    elif drift == "registry_id":
        registry["id"] += "-other"
    elif drift == "login_server":
        registry["properties"]["loginServer"] = "untrusted.invalid"
    elif drift == "run_id":
        properties["runId"] = "another-run"
    elif drift == "run_resource":
        run["id"] += "-other"
    elif drift == "run_task":
        properties["task"] += "-other"
    elif drift == "run_status":
        properties["status"] = "Running"
    elif drift == "duplicate_image":
        properties["outputImages"].append(deepcopy(image))
    else:
        image[{"output_registry": "registry"}.get(drift, drift)] = "unreviewed"
    with pytest.raises(publish.PublicationFailure):
        publish.publish_candidate(administrator, evidence)
    administrator.client.agents.create_version.assert_not_called()


@pytest.mark.parametrize("drift", [
    "candidate_definition", "candidate_metadata", "endpoint", "selector", "identity",
    "card", "role", "m365", "active_definition",
])
def test_candidate_readback_and_entire_active_parent_are_verified(operator, evidence, drift):
    administrator, parent, versions, _, _ = operator
    original = administrator.client.agents.create_version.side_effect

    def create(*args, **kwargs):
        candidate = original(*args, **kwargs)
        if drift == "candidate_definition":
            candidate.definition.environment_variables["UNREVIEWED"] = "changed"
        elif drift == "candidate_metadata":
            candidate.metadata["contoso-source-sha256"] = "e" * 64
        elif drift == "endpoint":
            parent["agent_endpoint"]["url"] = "https://unreviewed.invalid"
        elif drift == "selector":
            parent["agent_endpoint"]["version_selector"] = {}
        elif drift == "identity":
            parent["instance_identity"] = {}
        elif drift == "card":
            parent["agent_card"] = {}
        elif drift == "role":
            parent["roles"] = []
        elif drift == "m365":
            parent["m365_registration"] = {}
        else:
            versions["14"].definition.memory = "4Gi"
        return candidate

    administrator.client.agents.create_version.side_effect = create
    with pytest.raises(publish.PublicationFailure, match="drift|readback_mismatch"):
        publish.publish_candidate(administrator, evidence)
    assert administrator.client.agents.create_version.call_count == 1
    assert {item[0] for item in administrator.client.agents.method_calls} <= {
        "get", "get_version", "create_version",
    }


def test_preflight_failure_and_pinned_definition_drift_fail_before_creation(operator, evidence):
    administrator, _, versions, _, _ = operator
    administrator.run.side_effect = publish.AdministrationFailure("selected_operator_mismatch")
    with pytest.raises(publish.AdministrationFailure):
        publish.publish_candidate(administrator, evidence)
    administrator._get_json.assert_not_called()
    administrator.client.agents.get.assert_not_called()
    administrator.run.side_effect = None
    versions["14"].definition.cpu = "2"
    with pytest.raises(publish.PublicationFailure, match="active_router_definition_drift"):
        publish.publish_candidate(administrator, evidence)
    administrator.client.agents.create_version.assert_not_called()


@pytest.mark.parametrize("error", [
    ServiceRequestError("sensitive ambiguous service error"),
    HttpResponseError("sensitive response content"),
])
def test_ambiguous_creation_has_no_retry_restore_or_success_fallback(operator, evidence, error):
    administrator, _, _, _, _ = operator
    administrator.client.agents.create_version.side_effect = error
    with pytest.raises(publish.PublicationFailure, match="outcome_unconfirmed"):
        publish.publish_candidate(administrator, evidence)
    assert administrator.client.agents.create_version.call_count == 1
    assert {item[0] for item in administrator.client.agents.method_calls} == {
        "get", "get_version", "create_version",
    }


def test_apply_refuses_implicit_cli_profile(files, monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    with pytest.raises(SystemExit) as error:
        publish.main(arguments(files, apply=True))
    assert error.value.code == 2
    assert "AZURE_CONFIG_DIR" in capsys.readouterr().err


def test_apply_pins_profile_tenant_and_no_redirect_retry_transport(
    bindings, evidence, files, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    credential_factory, session_factory, client_factory = MagicMock(), MagicMock(), MagicMock()
    administrator_factory, publish_operation = Mock(), Mock()
    publish_operation.return_value = {"status": "verified-already-current"}
    for name, factory in (
        ("AzureCliCredential", credential_factory), ("AIProjectClient", client_factory),
        ("MotorsAdministrator", administrator_factory), ("publish_candidate", publish_operation),
    ):
        monkeypatch.setattr(publish, name, factory)
    monkeypatch.setattr(publish.requests, "Session", session_factory)
    assert publish.main(arguments(files, apply=True)) == 0
    credential_factory.assert_called_once_with(tenant_id=bindings.tenant_id, process_timeout=20)
    session = session_factory.return_value.__enter__.return_value
    credential = credential_factory.return_value.__enter__.return_value
    options = client_factory.call_args.kwargs
    assert session.trust_env is False
    assert options["endpoint"] == bindings.project_endpoint
    assert options["credential"] is credential
    assert options["retry_total"] == options["redirect_policy"].max_redirects == 0
    assert options["connection_timeout"] == 5 and options["read_timeout"] == 30
    assert options["transport"].session is session
    assert options["transport"]._session_owner is False
    assert publish.os.environ["AZURE_CONFIG_DIR"] == str(tmp_path)
    publish_operation.assert_called_once_with(administrator_factory.return_value, evidence)
    assert json.loads(capsys.readouterr().out)["status"] == "verified-already-current"


def test_cli_sanitizes_ambiguous_azure_failure_without_reporting_success(
    files, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        publish, "AzureCliCredential",
        Mock(side_effect=ServiceRequestError("sensitive exception with token")),
    )
    assert publish.main(arguments(files, apply=True)) == 1
    captured = capsys.readouterr()
    assert not captured.out and "sensitive" not in captured.err
    result = json.loads(captured.err)
    assert result["status"] == "failed"
    assert "outcome_unconfirmed" in result["error"]
    assert result["automatic_retry_performed"] is result["automatic_restore_performed"] is False
    assert result["independent_review_required_before_retry"] is True
