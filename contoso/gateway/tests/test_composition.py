import base64
import inspect
import json

from contoso.gateway_bootstrap import create_connections_app, create_runtime_app, create_app
import pytest
from contoso.identity import EntraAuthenticator, IdentitySettings
from contoso.ops.configure_gateway import configure

from contoso.gateway.tests.test_gateway import (
    TENANT, AUDIENCE, AWS_CLIENT, AWS_OID, AUTHORING_CLIENT, ADMIN_OID,
    AGENT_CLIENT, AGENT_OID, SECOND_AGENT_CLIENT, SECOND_AGENT_OID, GUEST, NATIVE_CLIENT,
)


def environment():
    return {
        "CONTOSO_ENTRA_TENANT_ID": TENANT, "CONTOSO_ENTRA_API_CLIENT_ID": AUDIENCE,
        "CONTOSO_AWS_REQUESTER_CLIENT_ID": AWS_CLIENT, "CONTOSO_AWS_REQUESTER_OID": AWS_OID,
        "CONTOSO_AUTHORING_CLIENT_ID": AUTHORING_CLIENT, "CONTOSO_AUTHORING_ADMIN_OID": ADMIN_OID,
        "CONTOSO_MANAGED_AGENT_CLIENT_ID": AGENT_CLIENT, "CONTOSO_MANAGED_AGENT_OID": AGENT_OID,
        "CONTOSO_MANAGED_AGENT_IDENTITIES": json.dumps([{"client_id": SECOND_AGENT_CLIENT, "oid": SECOND_AGENT_OID}]),
        "CONTOSO_GATEWAY_IDENTITY_OID": AWS_OID, "CONTOSO_PLATFORM_CALLER_IDS": json.dumps([AWS_OID]),
        "CONTOSO_STUDIO_ENVIRONMENT_ID": TENANT, "CONTOSO_STUDIO_STANDARD_SCHEMA": "ApprovedSTUDIO",
        "CONTOSO_STUDIO_HARNESS_ENDPOINT": "https://api.powerplatform.com/copilotstudio-a2a/approved",
        "CONTOSO_GATEWAY_URL": "https://gateway.azurewebsites.net",
        "CONTOSO_KNOWLEDGE_A2A_ENDPOINT": "https://contoso.services.ai.azure.com/api/projects/p/agents/knowledge/endpoint/protocols/a2a?api-version=v1",
        "CONTOSO_OAUTH_CLIENT_ID": NATIVE_CLIENT, "CONTOSO_OAUTH_GUEST_OID": GUEST,
        "CONTOSO_OAUTH_REDIRECT_URI": "https://gateway.azurewebsites.net/api/oauth/callback",
        "CONTOSO_OAUTH_CONTAINER_URL": "https://contoso.blob.core.windows.net/encrypted-tokens",
        "CONTOSO_OAUTH_STATE_KEY": base64.urlsafe_b64encode(b"x" * 32).decode(),
        "CONTOSO_OAUTH_STATE_NAMESPACE": "gateway-tests",
        "CONTOSO_FOUNDRY_PROJECT_ENDPOINT": "https://contoso.services.ai.azure.com/api/projects/p",
        "CONTOSO_ROUTER_SDK_AGENT_NAME": "router-sdk",
        "CONTOSO_ROUTER_A2A_ENDPOINT": "https://contoso.services.ai.azure.com/api/projects/p/agents/contoso-foundry-agent/endpoint/protocols/a2a?api-version=v1",
    }


def test_business_composition_preserves_ops_and_exact_guest_broker_grants():
    calls = {}
    def store(**kwargs):
        calls["store"] = kwargs
        return "encrypted-store"
    def broker(*args, **kwargs):
        calls["broker"] = args, kwargs
        return object()
    def network(*args, **kwargs):
        calls["network"] = args, kwargs
        async def invoke(**payload):
            raise AssertionError("Construction cannot invoke providers")
        return invoke
    app = create_runtime_app(
        environment(), authenticator=EntraAuthenticator(IdentitySettings(TENANT, AUDIENCE)),
        store_factory=store, credential_factory=object, broker_factory=broker, network_factory=network,
    )
    functions = app.get_functions()
    assert len(functions) == 7
    names = {function.get_function_name() for function in functions}
    assert names == {"contoso_invoke", "contoso_prepare", "contoso_status", "contoso_token", "contoso_callback", "contoso_unknown", "operations"}
    for function in functions:
        trigger = next(b for b in json.loads(function.get_function_json())["bindings"] if b["type"] == "httpTrigger")
        assert trigger["name"] in inspect.signature(function.get_user_function()).parameters
    args, kwargs = calls["broker"]
    assert args[0] == "encrypted-store"
    assert set(args[1]) == {"studio-standard", "studio-copilot-harness"}
    assert all(spec.guest_oid == GUEST and spec.client_id == NATIVE_CLIENT for spec in args[1].values())
    assert set(args[2]) == {AGENT_OID, SECOND_AGENT_OID}
    assert kwargs["preparer_providers"] == {ADMIN_OID: frozenset(args[1])}
    assert kwargs["cache_ttl"] == 2592000
    assert calls["network"][1]["catalog_caller_id"] == AWS_OID


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.settings = environment()
        self.settings["CONTOSO_PUBLICATION_BINDINGS"] = json.dumps({
            "subscription_id": TENANT, "resource_group": "approved",
            "blob_endpoint": "https://contoso.blob.core.windows.net/",
            "environment_values": {},
        })

    def call(self, method, url, payload=None):
        self.calls.append((method, url, payload))
        if method in ("PUT", "PATCH") or (method == "POST" and "appRoleAssignments" in url):
            return {}
        if "/config/appsettings/list" in url:
            return {"properties": self.settings.copy()}
        if "/config/authsettingsV2" in url:
            return {"properties": {
                "platform": {"enabled": True}, "globalValidation": {"requireAuthentication": True},
                "identityProviders": {"azureActiveDirectory": {"validation": {}}},
            }}
        if "/appRoleAssignments" in url:
            return {"value": []}
        if "/servicePrincipals(appId=" in url:
            client = url.split("appId='")[1].split("'")[0]
            return {
                "id": AWS_OID if client == AWS_CLIENT else AUDIENCE, "appId": client,
                "appRoles": [
                    {"id": AGENT_OID, "value": "CONTOSO.Connection.Use", "isEnabled": True, "allowedMemberTypes": ["Application"]},
                    {"id": AWS_OID, "value": "CONTOSO.Invoke", "isEnabled": True, "allowedMemberTypes": ["Application"]},
                ],
            }
        if "/servicePrincipals/" in url:
            oid = url.rsplit("/", 1)[1]
            return {"id": oid, "appId": AGENT_CLIENT if oid == AGENT_OID else SECOND_AGENT_CLIENT}
        if "/applications(" in url:
            return {"publicClient": {"redirectUris": ["http://localhost:8766/oauth/callback"]}}
        return {"properties": {"defaultHostName": "gateway.azurewebsites.net"}, "identity": {"principalId": AWS_OID}}


def records():
    return [{
        "name": name, "status": "active",
        "instance_identity": {"client_id": client, "principal_id": oid},
        "agent_endpoints": {"a2a": "https://contoso.services.ai.azure.com/a2a", "responses": "https://contoso.services.ai.azure.com/responses"},
    } for name, client, oid in (
        ("contoso-foundry-sdk-agent", AGENT_CLIENT, AGENT_OID),
        ("contoso-foundry-agent", SECOND_AGENT_CLIENT, SECOND_AGENT_OID),
        ("contoso-knowledge-agent", NATIVE_CLIENT, GUEST),
    )]


def test_cloud_configuration_preserves_secret_key_localhost_and_authenticated_routes():
    cloud = FakeCloud()
    result = configure(
        cloud, subscription=TENANT, resource_group="approved", function_name="gateway",
        oauth_client_id=NATIVE_CLIENT, guest_oid=GUEST, requester_client_id=AWS_CLIENT,
        records=records(), apply=True,
    )
    assert "CONTOSO_OAUTH_STATE_KEY" not in repr(result)
    settings = next(p["properties"] for m, u, p in cloud.calls if m == "PUT" and "/appsettings?" in u)
    assert settings["CONTOSO_OAUTH_STATE_KEY"] == cloud.settings["CONTOSO_OAUTH_STATE_KEY"]
    assert settings["CONTOSO_GATEWAY_PHASE"] == "connections"
    assert "CONTOSO_KNOWLEDGE_TRANSPORT" not in settings
    oauth = next(p for m, u, p in cloud.calls if m == "PATCH")
    assert oauth["publicClient"]["redirectUris"] == [
        "http://localhost:8766/oauth/callback", "https://gateway.azurewebsites.net/api/oauth/callback",
    ]
    auth = next(p["properties"] for m, u, p in cloud.calls if m == "PUT" and "/authsettingsV2?" in u)
    assert auth["globalValidation"]["requireAuthentication"] is True
    assert auth["globalValidation"]["excludedPaths"] == ["/api/oauth/callback"]
    assert len([1 for m, u, p in cloud.calls if m == "POST" and "/appRoleAssignments" in u]) == 2
    assert settings["CONTOSO_CONNECTION_PROVIDER_IDS"] == '["studio-copilot-harness"]'
    assert settings["CONTOSO_GATEWAY_A2A_ENABLED"] == "false"


def test_connections_phase_has_no_business_invoke_and_preserves_ops():
    app = create_connections_app(
        environment(), authenticator=EntraAuthenticator(IdentitySettings(TENANT, AUDIENCE)),
        store_factory=lambda **kwargs: object(),
        broker_factory=lambda *args, **kwargs: object(),
    )
    names = {function.get_function_name() for function in app.get_functions()}
    assert names == {"contoso_prepare", "contoso_status", "contoso_token", "contoso_callback", "operations"}
    with pytest.raises(ValueError):
        create_app({**environment(), "CONTOSO_GATEWAY_PHASE": "business"})


def test_connections_phase_separates_harness_and_standard_resource_audiences():
    captured = {}

    def broker(store, connections, *args, **kwargs):
        captured.update(connections)
        return object()

    create_connections_app(
        environment(), authenticator=EntraAuthenticator(IdentitySettings(TENANT, AUDIENCE)),
        store_factory=lambda **kwargs: object(), broker_factory=broker,
    )
    assert captured["studio-copilot-harness"].audience == "6f46e6ed-07bf-4616-80f0-47546850f654"
    assert captured["studio-standard"].audience == "https://api.powerplatform.com"


@pytest.mark.parametrize("transport", [None, "http-passthrough"])
def test_connections_composition_uses_only_trusted_transport_setting(monkeypatch, transport):
    from contoso import gateway_bootstrap
    from contoso.gateway import a2a
    seen = []
    class Bridge:
        def __init__(self, credential, **kwargs):
            seen.append(kwargs)
        async def forward(self, payload):
            raise AssertionError("No calls during composition")
    monkeypatch.setattr(gateway_bootstrap, "ManagedIdentityCredential", lambda: object())
    monkeypatch.setattr(a2a, "HostedA2ABridge", Bridge)
    values = {**environment(), "CONTOSO_GATEWAY_A2A_ENABLED": "true"}
    if transport is not None:
        values["CONTOSO_ROUTER_A2A_TRANSPORT"] = transport
    create_connections_app(
        values, authenticator=EntraAuthenticator(IdentitySettings(TENANT, AUDIENCE)),
        store_factory=lambda **kwargs: object(), broker_factory=lambda *args, **kwargs: object(),
    )
    assert seen == [{"endpoint": values["CONTOSO_ROUTER_A2A_ENDPOINT"],
                     "hosting_transport": "native" if transport is None else transport}]
