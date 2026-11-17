from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from contoso.ops import motors_admin as admin
from contoso.ops import motors_dependencies as dependencies


@pytest.fixture
def verifier(tmp_path):
    source = b"# Synthetic source\r\n\r\nVersion: 1\r\n"
    prompt = "Use only the synthetic source."
    for folder, name, content in (
        ("knowledge", "motors-supplier-delivery-terms-v1.md", source),
        ("prompts", "motors-supplier-v1.md", prompt.encode()),
    ):
        path = tmp_path / "contoso-demo" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    instructions = (
        prompt + "\n\n<approved-source-reference>\n"
        + source.decode() + "\n</approved-source-reference>"
    )
    group = (
        "/subscriptions/33333333-3333-4333-8333-333333333333/"
        "resourceGroups/rg-contoso-test"
    )
    tenant = "11111111-1111-4111-8111-111111111111"
    cfg = dependencies.DependencyBindings(
        schema_version=1, dataverse_origin="https://contoso.api.crm.dynamics.com",
        studio_bot_id="22222222-2222-4222-8222-222222222222",
        studio_schema_name="cr_test_motors",
        studio_source_component_id="44444444-4444-4444-8444-444444444444",
        studio_instructions_sha256=hashlib.sha256(instructions.encode()).hexdigest(),
        studio_published_on="2026-11-13T16:59:00Z",
        apim_resource_id=group + "/providers/Microsoft.ApiManagement/service/contoso-test",
        apim_principal_id="55555555-5555-4555-8555-555555555555",
        provider_name="contoso-studio-v2-pkce", connection_name="shared-owner",
        oauth_client_id="66666666-6666-4666-8666-666666666666",
    )
    operator = MagicMock(bindings=SimpleNamespace(
        tenant_id=tenant,
        project_resource_id=group + (
            "/providers/Microsoft.CognitiveServices/accounts/contoso/projects/contoso-project"
        ),
        studio_source_sha256=hashlib.sha256(source).hexdigest(),
    ))
    provider_id = cfg.apim_resource_id + "/authorizationProviders/" + cfg.provider_name
    connection_id = provider_id + "/authorizations/" + cfg.connection_name
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    values = {
        "bot": {
            "botid": cfg.studio_bot_id, "name": "Contoso Motors Supplier",
            "schemaname": cfg.studio_schema_name, "publishedon": cfg.studio_published_on,
            "configuration": json.dumps({"agentSettings": {"instructions": {"segments": [
                {"$kind": "StaticSegment", "value": instructions},
            ]}}, "privateSetting": "must-not-appear"}),
        },
        "component": {
            "botcomponentid": cfg.studio_source_component_id,
            "schemaname": cfg.studio_schema_name + ".file.motorssupplierdeliverytermsv1",
            "componenttype": 14, "_parentbotid_value": cfg.studio_bot_id,
        },
        cfg.apim_resource_id: {
            "id": cfg.apim_resource_id, "identity": {"principalId": cfg.apim_principal_id},
        },
        provider_id: {"id": provider_id, "properties": {
            "identityProvider": "oauth2pkce", "oauth2": {
                "redirectUrl": (
                    "https://authorization-manager.consent.azure-apim.net/redirect/apim/contoso-test"
                ),
                "grantTypes": {"authorizationCode": {
                    "clientId": cfg.oauth_client_id, "clientSecret": "must-not-appear",
                    "scopes": (
                        "https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A "
                        "offline_access"
                    ),
                    "authorizationUrl": (
                        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
                    ), "tokenUrl": token_url, "refreshUrl": token_url,
                }},
            },
        }},
        connection_id: {"id": connection_id, "properties": {
            "authorizationType": "OAuth2", "oauth2grantType": "AuthorizationCode",
            "status": "Connected",
        }},
        connection_id + "/accessPolicies": {"value": [{
            "id": connection_id + "/accessPolicies/apim-managed-identity",
            "properties": {"tenantId": tenant, "objectId": cfg.apim_principal_id},
        }]},
    }

    def get_json(url, scope):
        if "/botcomponents(" in url:
            assert scope == "https://contoso.crm.dynamics.com/.default"
            return deepcopy(values["component"])
        if "/bots(" in url:
            return deepcopy(values["bot"])
        resource_id = url.removeprefix("https://management.azure.com").split("?")[0]
        return deepcopy(values[resource_id])

    operator._get_json.side_effect = get_json
    response = MagicMock(status_code=200, history=[])
    response.iter_content.return_value = [source]
    operator.session.get.return_value.__enter__.return_value = response
    return dependencies.MotorsDependencies(operator, cfg), values, response, tmp_path


def test_remote_source_preserves_crlf_and_reports_only_verification(verifier):
    operation, _, _, root = verifier
    result = operation.studio_source(root=root)
    assert result["attached_source_matches_local_bytes"]
    assert result["inline_source_matches_local_bytes"]
    assert result["sources_published"] is False
    assert result["runtime_source_retrieval_proven"] is False
    assert result["draft_instructions_match_reviewed_publication"]
    assert "must-not-appear" not in json.dumps(result)
    assert operation.administrator.session.get.call_args.kwargs["allow_redirects"] is False
    assert operation.administrator.session.get.call_args.kwargs["timeout"] == (5, 30)


@pytest.mark.parametrize("drift", ["publication", "instructions", "parent", "attachment"])
def test_remote_source_or_publication_drift_cannot_pass(verifier, drift):
    operation, values, response, root = verifier
    if drift == "publication":
        values["bot"]["publishedon"] = "2026-11-14T16:59:00Z"
    elif drift == "instructions":
        values["bot"]["configuration"] = json.dumps({
            "agentSettings": {"instructions": {"segments": [
                {"$kind": "StaticSegment", "value": "Unreviewed prompt"},
            ]}},
        })
    elif drift == "parent":
        values["component"]["_parentbotid_value"] = "another-bot"
    else:
        response.iter_content.return_value = [b"Modified source"]
    with pytest.raises(admin.AdministrationFailure, match="drift"):
        operation.studio_source(root=root)


@pytest.mark.parametrize("size", [32768, 32769])
def test_remote_source_response_limit_is_enforced_at_exact_boundary(verifier, size):
    operation, _, response, root = verifier
    response.iter_content.return_value = [b"x" * size]
    expected_error = "remote_studio_source_drift" if size == 32768 else "studio_source_too_large"
    with pytest.raises(admin.AdministrationFailure, match=expected_error):
        operation.studio_source(root=root)


def test_remote_attachment_does_not_follow_redirects(verifier):
    operation, _, response, root = verifier
    response.status_code = 302
    with pytest.raises(admin.AdministrationFailure, match="studio_source_read_failed"):
        operation.studio_source(root=root)


def test_connection_checks_managed_callback_and_access_without_requesting_studio_token(verifier):
    operation, _, _, _ = verifier
    result = operation.connection()
    assert result["managed_connection_status"] == "Connected"
    assert result["managed_callback_configuration_verified"]
    assert result["apim_connection_access_verified"]
    assert result["studio_delegated_token_requested"] is False
    assert result["refresh_proven"] is False
    assert "must-not-appear" not in json.dumps(result)
    operation.administrator.session.get.assert_not_called()
    assert len(operation.administrator._get_json.call_args_list) == 4


@pytest.mark.parametrize("drift", [
    "status", "tenant", "callback", "scope", "identity", "pagination", "access", "provider-id",
])
def test_connection_drift_is_an_explicit_failure(verifier, drift):
    operation, values, _, _ = verifier
    cfg = operation.bindings
    provider_id = cfg.apim_resource_id + "/authorizationProviders/" + cfg.provider_name
    connection_id = provider_id + "/authorizations/" + cfg.connection_name
    oauth = values[provider_id]["properties"]["oauth2"]
    if drift == "status":
        values[connection_id]["properties"]["status"] = "Error"
    elif drift == "tenant":
        oauth["grantTypes"]["authorizationCode"]["tokenUrl"] = "https://login.microsoftonline.com/common"
    elif drift == "callback":
        oauth["redirectUrl"] = "https://legacy-function.example/api/oauth/callback"
    elif drift == "scope":
        oauth["grantTypes"]["authorizationCode"]["scopes"] += " another_scope"
    elif drift == "identity":
        values[cfg.apim_resource_id]["identity"]["principalId"] = "another-identity"
    elif drift == "pagination":
        values[connection_id + "/accessPolicies"]["nextLink"] = "https://untrusted.example"
    elif drift == "access":
        values[connection_id + "/accessPolicies"]["value"] = []
    else:
        values[provider_id]["id"] = "another-provider"
    with pytest.raises(admin.AdministrationFailure):
        operation.connection()


@pytest.mark.parametrize("change", [
    {"dataverse_origin": "https://evil.example/"},
    {"dataverse_origin": "https://contoso.api.crm.dynamics.com?redirect=evil"},
    {"studio_published_on": "2026-11-13T16:59:00"},
    {"connection_name": "../another"},
    {"studio_instructions_sha256": "unreviewed"},
])
def test_dependency_bindings_reject_untrusted_or_unpinned_inputs(verifier, change):
    operation, _, _, _ = verifier
    with pytest.raises(ValueError):
        replace(operation.bindings, **change)


def test_dependency_bindings_cannot_select_another_resource_group(verifier):
    operation, _, _, _ = verifier
    changed = replace(operation.bindings, apim_resource_id=operation.bindings.apim_resource_id.replace(
        "/rg-contoso-test/", "/rg-unapproved/",
    ))
    with pytest.raises(ValueError, match="selected subscription and resource group"):
        dependencies.MotorsDependencies(operation.administrator, changed)


def test_binding_file_rejects_duplicate_and_unknown_fields(verifier, tmp_path):
    operation, _, _, _ = verifier
    path = tmp_path / "bindings.json"
    text = json.dumps(asdict(operation.bindings))
    path.write_text(text[:-1] + ', "schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        dependencies.DependencyBindings.load(path)
    path.write_text(json.dumps(asdict(operation.bindings) | {"token": "not-allowed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        dependencies.DependencyBindings.load(path)


def test_cli_rejects_an_implicit_profile_before_credentials(monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    credential = Mock(side_effect=AssertionError("Credential access forbidden"))
    monkeypatch.setattr(dependencies, "AzureCliCredential", credential)
    with pytest.raises(SystemExit) as error:
        dependencies.main(["--bindings", "unused.json", "--dependencies", "unused.json", "connection"])
    assert error.value.code == 2
    assert "AZURE_CONFIG_DIR" in capsys.readouterr().err
    credential.assert_not_called()


def test_cli_checks_private_operator_before_connection_with_pinned_transport(
    verifier, tmp_path, monkeypatch, capsys,
):
    operation, _, _, _ = verifier
    cfg = SimpleNamespace(
        tenant_id=operation.administrator.bindings.tenant_id,
        project_endpoint="https://contoso.services.ai.azure.com/api/projects/contoso-project",
    )
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(admin.Bindings, "load", Mock(return_value=cfg))
    monkeypatch.setattr(dependencies.DependencyBindings, "load", Mock(return_value=operation.bindings))
    factories = {name: MagicMock() for name in (
        "AzureCliCredential", "RequestsTransport", "RedirectPolicy", "AIProjectClient",
        "MotorsDependencies",
    )}
    for name, factory in factories.items():
        monkeypatch.setattr(dependencies, name, factory)
    administrator = MagicMock()
    monkeypatch.setattr(admin, "MotorsAdministrator", administrator)
    events = []
    administrator.return_value.run.side_effect = lambda name: events.append(name)
    check = factories["MotorsDependencies"].return_value
    check.connection.side_effect = lambda: events.append("connection") or {
        "managed_connection_status": "Connected",
    }
    session_factory = MagicMock()
    monkeypatch.setattr(dependencies.requests, "Session", session_factory)
    assert dependencies.main([
        "--bindings", "reviewed.json", "--dependencies", "reviewed-dependencies.json", "connection",
    ]) == 0
    assert events == ["preflight", "connection"]
    factories["AzureCliCredential"].assert_called_once_with(tenant_id=cfg.tenant_id, process_timeout=20)
    factories["RedirectPolicy"].assert_called_once_with(redirect_max=0)
    session = session_factory.return_value.__enter__.return_value
    assert session.trust_env is False
    factories["RequestsTransport"].assert_called_once_with(session=session, session_owner=False)
    assert factories["AIProjectClient"].call_args.kwargs["retry_total"] == 0
    result = json.loads(capsys.readouterr().out)
    assert result["function_called"] is False
    assert result["function_retirement_ready"] is False
