"""Explicit publication-only phase, protected by the selected operator's JWT."""

import asyncio
import json
import logging
import os
import re

import azure.functions as func
from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential

from contoso_backend.errors import ChatFailure
from contoso.configuration import HARNESS_AUDIENCE, identifier, required
from contoso.identity import EntraAuthenticator, IdentitySettings
from contoso.gateway_probe import SERVICES, probe_service
from contoso.publication import PublicationFailure
from contoso.publication.runtime import create_executor, operator_config, unique_fields as _unique


def create_publication_app(env, *, authenticator=None, executor_factory=None, probe=None, app=None):
    tenant = identifier(env, "CONTOSO_ENTRA_TENANT_ID")
    audience = identifier(env, "CONTOSO_ENTRA_API_CLIENT_ID")
    client_id = identifier(env, "CONTOSO_AUTHORING_CLIENT_ID")
    admin_oid = identifier(env, "CONTOSO_AUTHORING_ADMIN_OID")
    auth = authenticator or EntraAuthenticator(IdentitySettings(tenant, audience))
    app = app if app is not None else func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
    logger = logging.getLogger("contoso.publication")

    def make_executor():
        return create_executor(operator_config(env))

    factory = executor_factory or make_executor

    @app.route(route="ops/{operation}", methods=["POST"], trigger_arg_name="request")
    async def operations(request: func.HttpRequest):
        operation = request.route_params.get("operation")
        if operation not in (
            "preflight", "sources", "publish-service", "records", "metadata", "probe", "link-probe", "diagnostics",
            "oauth-readiness", "oauth-callback-diagnostics", "a2a-probe", "a2a-session-snapshot",
        ):
            return _response(404, {"error": "not_found"})
        try:
            caller = await asyncio.to_thread(
                auth.verify, request.headers.get("Authorization", ""),
                allowed_clients=frozenset({client_id}), allowed_callers=frozenset({admin_oid}),
                delegated_scope="CONTOSO.Deploy", application_role="CONTOSO.DeploymentAdmin",
            )
            if caller.kind != "user":
                raise ChatFailure(403, "forbidden", "Only the selected deployment operator is authorized.")
            body = request.get_body()
            if len(body) > 4096:
                raise ValueError("Publication request is too large")
            payload = json.loads(body, object_pairs_hook=_unique)
            expected = (
                {"service", "process_user_agent"} if operation == "publish-service"
                else {"process_user_agent"} if operation == "diagnostics"
                else {"service"} if operation in ("probe", "link-probe") else set()
            )
            if operation == "oauth-callback-diagnostics":
                expected = {"correlation_id"}
            if not isinstance(payload, dict) or set(payload) != expected:
                raise ValueError("Invalid publication operation fields")
            if operation in ("publish-service", "probe", "link-probe"):
                if not isinstance(payload["service"], str) or payload["service"] not in SERVICES:
                    raise ValueError("Only the three fixed services may be published")
                if operation == "link-probe" and payload["service"] == "contoso-knowledge-agent":
                    raise ValueError("Only the two Routers support diagnostic linking")
            if operation in ("publish-service", "diagnostics"):
                agent = payload["process_user_agent"]
                if not isinstance(agent, str) or not re.fullmatch(r"[\x20-\x7e]{1,256}", agent):
                    raise ValueError("A printable one-shot process user agent is required")
            if operation == "a2a-session-snapshot":
                from contoso.gateway_session_probe import snapshot_native_sessions
                result = await snapshot_native_sessions(
                    await asyncio.to_thread(operator_config, env),
                    endpoint=required(env, "CONTOSO_ROUTER_A2A_ENDPOINT"),
                )
                if result["status"] == "failed":
                    return _response(503, result)
            elif operation == "a2a-probe":
                from contoso.gateway_a2a_probe import probe_native_a2a
                result = await probe_native_a2a(
                    await asyncio.to_thread(operator_config, env),
                    endpoint=required(env, "CONTOSO_ROUTER_A2A_ENDPOINT"),
                    hosting_transport=env.get("CONTOSO_ROUTER_A2A_TRANSPORT", "native"),
                )
                if result["status"] == "failed":
                    return _response(503, result)
            elif operation == "oauth-callback-diagnostics":
                correlation = payload["correlation_id"]
                if not isinstance(correlation, str) or not re.fullmatch(r"[0-9a-f]{32}", correlation):
                    raise ValueError("Invalid callback correlation")
                from contoso.gateway.callback_diagnostics import read_callback
                result = await asyncio.to_thread(read_callback, correlation)
                if result is None:
                    return _response(404, {"error": "callback_metadata_not_available"})
            elif operation == "oauth-readiness":
                try:
                    candidate = await asyncio.to_thread(create_connections_app, env)
                    result = {"status": "oauth-runtime-ready", "functions": [
                        function.get_function_name() for function in candidate.get_functions()
                    ]}
                except Exception as error:
                    module = getattr(error, "name", None)
                    safe_module = (module if isinstance(module, str)
                                   and re.fullmatch(r"[A-Za-z0-9_.]{1,128}", module) else None)
                    return _response(503, {
                        "error": "oauth_runtime_unavailable",
                        "exception_type": type(error).__name__, "missing_module": safe_module,
                    })
            elif operation in ("probe", "link-probe"):
                config = await asyncio.to_thread(operator_config, env)
                result = await asyncio.to_thread(
                    probe or probe_service, config, payload["service"],
                    link=operation == "link-probe",
                )
            elif operation == "preflight":
                executor = await asyncio.to_thread(factory)
                await asyncio.to_thread(executor.preflight)
                result = {"status": "private-preflight-passed"}
            elif operation == "sources":
                executor = await asyncio.to_thread(factory)
                result = await asyncio.to_thread(executor.publish_sources)
            elif operation == "records":
                executor = await asyncio.to_thread(factory)
                result = await asyncio.to_thread(executor.get_agent_records)
            elif operation == "metadata":
                executor = await asyncio.to_thread(factory)
                records = await asyncio.to_thread(executor.get_agent_records, include_endpoint=False)
                result = {
                    "record_contract": 1, "endpoint_validation": "not_requested", "records": records,
                }
            elif operation == "diagnostics":
                executor = await asyncio.to_thread(factory)
                result = await asyncio.to_thread(
                    executor.diagnostic_self_test,
                    process_user_agent=payload["process_user_agent"],
                )
                if result.get("status") == "failed":
                    logger.error("CONTOSO private deployment diagnostic failed")
                    return _response(503, result)
            else:
                executor = await asyncio.to_thread(factory)
                job = await asyncio.to_thread(
                    executor.publish_service, payload["service"],
                    process_user_agent=payload["process_user_agent"],
                )
                result = job.as_dict()
                if result["status"] == "failed":
                    logger.error("CONTOSO private deployment failed: %s", result["error_code"])
                    return _response(503, result)
            logger.info("CONTOSO operator operation completed: %s", operation)
            return _response(200, result)
        except ChatFailure as error:
            return _response(error.status, {"error": error.code})
        except (ValueError, json.JSONDecodeError):
            return _response(400, {"error": "invalid_publication_request"})
        except PublicationFailure as error:
            logger.error("CONTOSO publication operation failed: %s", str(error))
            return _response(503, {"error": str(error)})
        except AzureError:
            logger.error("CONTOSO publication private dependency failed")
            return _response(503, {"error": "private_dependency_unavailable"})

    return app


def _response(status, body):
    return func.HttpResponse(
        json.dumps(body, ensure_ascii=True), status_code=status,
        mimetype="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def create_runtime_app(
    env, *, authenticator=None, store_factory=None, credential_factory=None,
    network_factory=None, broker_factory=None, probe=None,
):
    """Production business routes and existing protected operator routes."""
    from contoso.configuration import RouterSettings
    from contoso.connections import DelegatedConnectionBroker, ProviderConnection
    from contoso.gateway import GatewaySettings, create_function_app, create_managed_identity_store
    from contoso.router.core import Capability, STUDIOVariant
    from contoso.network import FoundryNetworkClient

    values = dict(env)
    values.setdefault("CONTOSO_TENANT_ID", required(values, "CONTOSO_ENTRA_TENANT_ID"))
    values.setdefault("CONTOSO_GATEWAY_AUDIENCE", required(values, "CONTOSO_ENTRA_API_CLIENT_ID"))
    settings = GatewaySettings.from_env(values)
    library = RouterSettings.from_environment(values)
    catalog = library.catalog()
    gateway_oid = identifier(values, "CONTOSO_GATEWAY_IDENTITY_OID")
    connections = {}
    for variant in STUDIOVariant:
        entry = catalog.lookup(gateway_oid, Capability.SUPPLIER_DELIVERY_TERMS, variant=variant).entry
        spec = ProviderConnection.from_catalog(
            entry, client_id=identifier(values, "CONTOSO_OAUTH_CLIENT_ID"),
            guest_oid=identifier(values, "CONTOSO_OAUTH_GUEST_OID"),
            redirect_uri=required(values, "CONTOSO_OAUTH_REDIRECT_URI"),
        )
        connections[spec.provider_id] = spec
    store = (store_factory or create_managed_identity_store)(
        container_url=required(values, "CONTOSO_OAUTH_CONTAINER_URL"),
        encoded_key=required(values, "CONTOSO_OAUTH_STATE_KEY"),
        namespace=required(values, "CONTOSO_OAUTH_STATE_NAMESPACE"),
    )
    broker = (broker_factory or DelegatedConnectionBroker)(
        store, connections,
        {oid: frozenset(connections) for _, oid in settings.token_identities},
        preparer_providers={settings.authoring_admin_oid: frozenset(connections)},
        cache_ttl=2592000,
    )
    credential = (credential_factory or ManagedIdentityCredential)()
    network = (network_factory or FoundryNetworkClient)(
        credential,
        project_endpoint=required(values, "CONTOSO_FOUNDRY_PROJECT_ENDPOINT"),
        sdk_agent_name=required(values, "CONTOSO_ROUTER_SDK_AGENT_NAME"),
        a2a_endpoint=required(values, "CONTOSO_ROUTER_A2A_ENDPOINT"),
        catalog=catalog, catalog_caller_id=gateway_oid,
    )
    app = create_function_app(
        settings, broker=broker, network_client=network, authenticator=authenticator,
    )
    return create_publication_app(values, authenticator=authenticator, probe=probe, app=app)


def create_connections_app(env, *, authenticator=None, store_factory=None, broker_factory=None):
    """OAuth persistence only; no business network protocol is enabled."""
    from contoso.connections import DelegatedConnectionBroker, ProviderConnection
    from contoso.connections.oauth import STANDARD_SCOPE, HARNESS_SCOPE
    from contoso.gateway import GatewaySettings, create_function_app, create_managed_identity_store
    from contoso.router.core import STUDIOVariant

    values = dict(env)
    values.setdefault("CONTOSO_TENANT_ID", required(values, "CONTOSO_ENTRA_TENANT_ID"))
    values.setdefault("CONTOSO_GATEWAY_AUDIENCE", required(values, "CONTOSO_ENTRA_API_CLIENT_ID"))
    settings = GatewaySettings.from_env(values)
    connections = {}
    selected_providers = json.loads(values.get(
        "CONTOSO_CONNECTION_PROVIDER_IDS", '["studio-standard","studio-copilot-harness"]',
    ), object_pairs_hook=_unique)
    if (not isinstance(selected_providers, list) or not selected_providers
            or any(p not in ("studio-standard", "studio-copilot-harness") for p in selected_providers)
            or len(set(selected_providers)) != len(selected_providers)):
        raise ValueError("Explicit supported connection provider IDs are required")
    for variant, scope in (
        (STUDIOVariant.STANDARD, STANDARD_SCOPE), (STUDIOVariant.COPILOT_HARNESS, HARNESS_SCOPE),
    ):
        provider_id = "studio-" + variant.value
        if provider_id not in selected_providers:
            continue
        connections[provider_id] = ProviderConnection(
            provider_id, settings.tenant_id, identifier(values, "CONTOSO_OAUTH_CLIENT_ID"),
            identifier(values, "CONTOSO_OAUTH_GUEST_OID"), required(values, "CONTOSO_OAUTH_REDIRECT_URI"),
            variant,
            HARNESS_AUDIENCE if variant == STUDIOVariant.COPILOT_HARNESS else "https://api.powerplatform.com",
            frozenset({scope}),
        )
    store = (store_factory or create_managed_identity_store)(
        container_url=required(values, "CONTOSO_OAUTH_CONTAINER_URL"),
        encoded_key=required(values, "CONTOSO_OAUTH_STATE_KEY"),
        namespace=required(values, "CONTOSO_OAUTH_STATE_NAMESPACE"),
    )
    broker = (broker_factory or DelegatedConnectionBroker)(
        store, connections, {oid: frozenset(connections) for _, oid in settings.token_identities},
        preparer_providers={settings.authoring_admin_oid: frozenset(connections)},
        cache_ttl=2592000,
    )

    async def disabled_network(**kwargs):
        raise RuntimeError("Business invocation is not enabled")

    a2a_bridge = None
    bridge_enabled = values.get("CONTOSO_GATEWAY_A2A_ENABLED", "false")
    if bridge_enabled not in ("true", "false"):
        raise ValueError("A2A bridge enablement must be explicit")
    if bridge_enabled == "true":
        from contoso.gateway.a2a import HostedA2ABridge
        a2a_bridge = HostedA2ABridge(
            ManagedIdentityCredential(), endpoint=required(values, "CONTOSO_ROUTER_A2A_ENDPOINT"),
            hosting_transport=values.get("CONTOSO_ROUTER_A2A_TRANSPORT", "native"),
        )
    app = create_function_app(
        settings, broker=broker, network_client=disabled_network,
        authenticator=authenticator, enable_invoke=False, a2a_bridge=a2a_bridge,
        enable_unknown_route=False,
    )
    return create_publication_app(values, authenticator=authenticator, app=app)


def create_app(env=None):
    settings = os.environ if env is None else env
    phase = required(settings, "CONTOSO_GATEWAY_PHASE")
    if phase == "publication":
        return create_publication_app(settings)
    if phase == "connections":
        return create_connections_app(settings)
    raise ValueError("An explicit supported Gateway phase is required")
