"""Production container composition. Authentication never falls back to operator credentials."""

import os
from urllib.parse import urlsplit

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobClient

from contoso.knowledge import KnowledgeHost
from contoso.configuration import (
    HarnessRouterSettings, RouterSettings, demo_router, endpoint,
    identity_link_transport, platform_callers, required,
)
from contoso.router.core import Capability, CatalogError, STUDIOVariant, Protocol, _https_origin
from contoso.router.hosted import HostedRouter, current_platform_context
from contoso.router.runtime import RouterRuntime
from contoso.providers import (
    A2AAdapter, FoundryResponsesTransport, StandardSDKAdapter,
)
from contoso.token_client import ProviderTokens


def build_host(env, credential):
    role = required(env, "CONTOSO_RUNTIME_ROLE")
    if role == "identity-probe":
        from contoso.identity_probe import build_probe_host

        return build_probe_host()
    if role == "identity-link-probe":
        from contoso.identity_link import IdentityLinkProbe

        protocol, target = identity_link_transport(env)
        transport = None
        if protocol == Protocol.FOUNDRY_RESPONSES:
            transport = FoundryResponsesTransport(
                endpoint=target, approved_origins=frozenset({_https_origin(target)}),
                overall_timeout=40,
                platform_headers_factory=lambda: current_platform_context().platform_headers(),
            )
        return IdentityLinkProbe(
            credential,
            endpoint=target,
            allowed_callers=platform_callers(env),
            transport=transport,
        ).build_host()
    if role == "knowledge":
        storage = endpoint(env, "CONTOSO_SOURCE_BLOB_ENDPOINT", ".blob.core.windows.net")
        if urlsplit(storage).path:
            raise CatalogError("The private source setting must be a Blob service origin")
        blob = BlobClient(
            account_url=storage,
            container_name="sources",
            blob_name="knowledge-returnables.md",
            credential=credential,
            connection_timeout=5, read_timeout=15, retry_total=0,
        )
        return KnowledgeHost(
            blob,
            source_sha256=required(env, "CONTOSO_KNOWLEDGE_SOURCE_SHA256"),
            allowed_callers=platform_callers(env),
        ).build_host()
    if role != "router":
        raise CatalogError("The configured Hosted runtime role is unsupported")
    scope = env.get("CONTOSO_ROUTER_SCOPE", "catalog")
    if scope == "studio-harness":
        harness = HarnessRouterSettings.from_environment(env)
        tokens = ProviderTokens(
            credential, tenant_id=harness.tenant_id,
            gateway_url=harness.gateway_url, gateway_api_id=harness.gateway_api_id,
        )
        runtime = RouterRuntime(
            harness.catalog(), demo_router(capability=Capability.SUPPLIER_DELIVERY_TERMS),
            {"studio-copilot-harness": A2AAdapter(
                approved_origins=frozenset({_https_origin(harness.harness_endpoint)}),
            )},
            tokens,
        )
        return HostedRouter(runtime, fixed_variant=STUDIOVariant.COPILOT_HARNESS).build_host()
    if scope != "catalog":
        raise CatalogError("The configured Router scope is unsupported")
    settings = RouterSettings.from_environment(env)
    tokens = ProviderTokens(
        credential, tenant_id=settings.tenant_id,
        gateway_url=settings.gateway_url, gateway_api_id=settings.gateway_api_id,
    )
    power_platform_origins = frozenset({
        "https://api.powerplatform.com", _https_origin(settings.standard_endpoint),
    })
    knowledge_origin = f"https://{urlsplit(settings.knowledge_endpoint).netloc}"
    runtime = RouterRuntime(
        settings.catalog(),
        demo_router(),
        {
            "knowledge-demo": A2AAdapter(
                approved_origins=frozenset({knowledge_origin}),
                platform_headers_factory=lambda: current_platform_context().platform_headers(),
            ),
            "studio-standard": StandardSDKAdapter(
                approved_origins=power_platform_origins,
                environment_id=settings.environment_id,
                agent_identifier=settings.standard_schema,
            ),
            "studio-copilot-harness": A2AAdapter(approved_origins=power_platform_origins),
        },
        tokens,
    )
    return HostedRouter(runtime).build_host()


def main():
    build_host(os.environ, ManagedIdentityCredential()).run(port=8088)


if __name__ == "__main__":
    main()
