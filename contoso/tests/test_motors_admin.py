from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from azure.ai.projects.models import AgentSessionStatus
import pytest

from contoso.ops import motors_admin as admin
from contoso.ops.create_prompt import build_definition


@pytest.fixture
def bindings():
    return admin.Bindings(
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
def operator(bindings, monkeypatch):
    definitions = {
        admin.ROUTER: {"kind": "hosted", "environment_variables": {
            "PRIVATE_SETTING": "must-not-appear-in-output",
        }},
        admin.LOGISTICS: build_definition(bindings.logistics_model).as_dict(),
    }
    bindings = replace(
        bindings, router_definition_sha256=admin.digest(definitions[admin.ROUTER]),
        logistics_definition_sha256=admin.digest(definitions[admin.LOGISTICS]),
    )
    agents = {}
    for name, version, principal, protocols in (
        (admin.ROUTER, bindings.router_version, bindings.router_principal_id,
         ["a2a", "activity", "invocations", "responses"]),
        (admin.LOGISTICS, bindings.logistics_version, bindings.logistics_principal_id,
         ["a2a", "responses"]),
    ):
        schemes = [{"type": "Entra", "isolation_key_source": {"kind": "Entra"}}]
        if name == admin.ROUTER:
            schemes.append({"type": "BotServiceRbac"})
        agents[name] = {
            "name": name, "state": "enabled",
            "instance_identity": {"principal_id": principal},
            "agent_endpoint": {
                "version_selector": {"version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100},
                ]},
                "protocols": protocols, "authorization_schemes": schemes,
            },
        }
    client = Mock()
    client.agents.get.side_effect = lambda name: SimpleNamespace(
        as_dict=lambda: deepcopy(agents[name]),
    )
    client.agents.get_version.side_effect = lambda name, version: SimpleNamespace(
        name=name, version=version,
        definition=SimpleNamespace(as_dict=lambda: deepcopy(definitions[name])),
    )
    operation = admin.MotorsAdministrator(bindings, Mock(), Mock(), client)

    def get_json(url, scope):
        if url.startswith("https://graph.microsoft.com/"):
            return {"id": bindings.operator_object_id}
        resource_id = url.removeprefix("https://management.azure.com").split("?")[0]
        return {"id": resource_id, "properties": {"publicNetworkAccess": "Disabled"}}

    monkeypatch.setattr(operation, "_get_json", get_json)
    monkeypatch.setattr(admin.socket, "getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("10.77.2.23", 443)),
    ])
    return operation, agents, definitions


@pytest.mark.parametrize("change", [
    {"schema_version": True},
    {"router_version": "@latest"},
    {"logistics_version": "5?api-version=v1"},
    {"project_endpoint": "https://another.services.ai.azure.com/api/projects/contoso-project"},
    {"project_resource_id": "/subscriptions/../../outside"},
    {"operator_object_id": 42},
])
def test_invalid_or_unpinned_bindings_fail_closed(bindings, change):
    with pytest.raises(ValueError):
        replace(bindings, **change)


def test_binding_file_rejects_duplicate_fields_and_unknown_settings(bindings, tmp_path):
    path = tmp_path / "bindings.json"
    text = json.dumps(asdict(bindings))
    path.write_text(text[:-1] + ', "schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        admin.Bindings.load(path)
    path.write_text(json.dumps(asdict(bindings) | {"client_secret": "not-allowed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        admin.Bindings.load(path)


def test_preflight_reads_pinned_agents_without_exposing_environment_or_calling_function(operator):
    operation, _, _ = operator
    result = operation.run("preflight")
    assert result["private_project_addresses"] == ["10.77.2.23"]
    assert {record["version"] for record in result["records"]} == {"14", "5"}
    assert "must-not-appear-in-output" not in json.dumps(result)
    assert "environment_variables" not in json.dumps(result)
    assert [call.args for call in operation.client.agents.get_version.call_args_list] == [
        (admin.ROUTER, "14"), (admin.LOGISTICS, "5"),
    ]
    assert set(operation.client.agents.method_calls[i][0] for i in range(4)) == {
        "get", "get_version",
    }


def test_operator_mismatch_is_rejected_before_any_agent_read(operator, monkeypatch):
    operation, _, _ = operator
    monkeypatch.setattr(operation, "_get_json", lambda *args: {"id": "different-user"})
    with pytest.raises(admin.AdministrationFailure, match="selected_operator_mismatch"):
        operation.run("records")
    operation.client.agents.get.assert_not_called()


@pytest.mark.parametrize("addresses", [[], ["20.1.1.1"], ["127.0.0.1"], [
    "10.77.2.23", "20.1.1.1",
]])
def test_private_dns_cannot_fall_back_to_public_or_loopback(operator, monkeypatch, addresses):
    operation, _, _ = operator
    monkeypatch.setattr(admin.socket, "getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", (address, 443)) for address in addresses
    ])
    with pytest.raises(admin.AdministrationFailure, match="private_project_dns_required"):
        operation.run("preflight")
    operation.client.agents.get.assert_not_called()


def test_public_access_drift_is_rejected_before_data_plane(operator, monkeypatch):
    operation, _, _ = operator
    original = operation._get_json

    def get_json(url, scope):
        record = original(url, scope)
        if "properties" in record:
            record["properties"]["publicNetworkAccess"] = "Enabled"
        return record

    monkeypatch.setattr(operation, "_get_json", get_json)
    with pytest.raises(admin.AdministrationFailure, match="private_foundry_required"):
        operation.run("diagnostics")
    operation.client.agents.get.assert_not_called()


@pytest.mark.parametrize("drift", ["selector", "identity", "definition", "authorization"])
def test_agent_drift_is_not_reported_as_success(operator, drift):
    operation, agents, definitions = operator
    if drift == "selector":
        agents[admin.ROUTER]["agent_endpoint"]["version_selector"] = {}
    elif drift == "identity":
        agents[admin.ROUTER]["instance_identity"]["principal_id"] = "another-principal"
    elif drift == "definition":
        definitions[admin.ROUTER]["environment_variables"]["UNREVIEWED"] = "drift"
    else:
        agents[admin.ROUTER]["agent_endpoint"]["authorization_schemes"] = [{"type": "Anonymous"}]
    with pytest.raises(admin.AdministrationFailure, match="drift"):
        operation.run("metadata")


def test_source_check_verifies_prompt_embedding_but_does_not_claim_remote_studio_acceptance(
    operator, monkeypatch,
):
    operation, _, _ = operator
    studio = admin.ROOT / "contoso-demo" / "knowledge" / "motors-supplier-delivery-terms-v1.md"
    monkeypatch.setattr(admin, "source_archive", lambda root: b"reviewed-source")
    operation.bindings = replace(
        operation.bindings,
        router_source_sha256=admin.hashlib.sha256(b"reviewed-source").hexdigest(),
        studio_source_sha256=admin.hashlib.sha256(studio.read_bytes()).hexdigest(),
    )
    result = operation.run("sources")["sources"]
    assert result["logistics_embedded_reference_verified"]
    assert result["remote_studio_source_checked"] is False
    assert result["sources_published"] is False


def test_snapshot_is_restricted_to_explicit_session_and_pinned_version(operator):
    operation, _, _ = operator
    session = SimpleNamespace(
        agent_session_id="known-session",
        version_indicator=SimpleNamespace(as_dict=lambda: {
            "type": "version_ref", "agent_version": "11",
        }),
        status=AgentSessionStatus.IDLE, created_at=datetime.now(timezone.utc),
    )
    operation.client.agents.get_session.return_value = session
    with pytest.raises(admin.AdministrationFailure, match="session_version_mismatch"):
        operation.run("a2a-session-snapshot", agent=admin.ROUTER, session_id="known-session")
    session.version_indicator.as_dict = lambda: {"type": "version_ref", "agent_version": "14"}
    result = operation.run(
        "a2a-session-snapshot", agent=admin.ROUTER, session_id="known-session",
    )
    assert result["session"]["status"] == AgentSessionStatus.IDLE.value
    assert set(result["session"]) == {
        "agent_name", "agent_session_id", "version", "status", "created_at",
    }
    operation.client.agents.stop_session.assert_not_called()
    operation.client.agents.list_sessions.assert_not_called()


def test_http_read_rejects_redirects_without_disclosing_token(bindings):
    response = MagicMock(status_code=302, history=[])
    session = Mock()
    session.get.return_value.__enter__ = Mock(return_value=response)
    session.get.return_value.__exit__ = Mock(return_value=False)
    credential = Mock()
    credential.get_token.return_value.token = "private-token"
    operation = admin.MotorsAdministrator(bindings, credential, session, Mock())
    with pytest.raises(admin.AdministrationFailure, match="administration_read_failed"):
        operation._get_json("https://graph.microsoft.com/v1.0/me", "scope")
    assert session.get.call_args.kwargs["allow_redirects"] is False
    assert session.get.call_args.kwargs["timeout"] == (5, 30)


@pytest.mark.parametrize("size", [262144, 262145])
def test_http_read_enforces_exact_response_size_boundary(bindings, size):
    response = MagicMock(status_code=200, history=[])
    response.iter_content.return_value = [b"{}" + b" " * (size - 2)]
    session = MagicMock()
    session.get.return_value.__enter__.return_value = response
    credential = Mock()
    credential.get_token.return_value.token = "private-token"
    operation = admin.MotorsAdministrator(bindings, credential, session, Mock())
    if size == 262144:
        assert operation._get_json("https://graph.microsoft.com/v1.0/me", "scope") == {}
    else:
        with pytest.raises(admin.AdministrationFailure, match="administration_response_too_large"):
            operation._get_json("https://graph.microsoft.com/v1.0/me", "scope")


def test_cli_cannot_use_an_implicit_profile(monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    credential = Mock(side_effect=AssertionError("Credential access is forbidden"))
    monkeypatch.setattr(admin, "AzureCliCredential", credential)
    with pytest.raises(SystemExit) as error:
        admin.main(["--bindings", "unused.json", "records"])
    assert error.value.code == 2
    assert "AZURE_CONFIG_DIR" in capsys.readouterr().err
    credential.assert_not_called()


def test_cli_pins_tenant_and_transport_without_modifying_default_profile(
    bindings, tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(asdict(bindings)), encoding="utf-8")
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    credential_factory, session_factory, client_factory = MagicMock(), MagicMock(), MagicMock()
    transport_factory, redirect_factory, administrator = MagicMock(), MagicMock(), MagicMock()
    administrator.return_value.run.return_value = {"status": "verified-read-only-operation"}
    for name, factory in (
        ("AzureCliCredential", credential_factory), ("AIProjectClient", client_factory),
        ("RequestsTransport", transport_factory), ("RedirectPolicy", redirect_factory),
        ("MotorsAdministrator", administrator),
    ):
        monkeypatch.setattr(admin, name, factory)
    monkeypatch.setattr(admin.requests, "Session", session_factory)
    assert admin.main(["--bindings", str(path), "records"]) == 0
    credential_factory.assert_called_once_with(tenant_id=bindings.tenant_id, process_timeout=20)
    session = session_factory.return_value.__enter__.return_value
    assert session.trust_env is False
    transport_factory.assert_called_once_with(session=session, session_owner=False)
    redirect_factory.assert_called_once_with(redirect_max=0)
    assert client_factory.call_args.kwargs["retry_total"] == 0
    assert client_factory.call_args.kwargs["endpoint"] == bindings.project_endpoint
    assert json.loads(capsys.readouterr().out)["status"] == "verified-read-only-operation"
