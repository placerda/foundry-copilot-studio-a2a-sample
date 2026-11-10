"""Administrator-owned catalog composition, independent of prompt content."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from contoso.router.core import (
    AuthMetadata, AuthScheme, CallerGrant, Capability, Catalog, CatalogEntry,
    CatalogError, STUDIOVariant, Protocol, SourceMetadata, _https_origin,
)
from contoso.router.runtime import DemoQuestionRouter
from contoso.providers import standard_catalog_endpoint


HARNESS_AUDIENCE = "6f46e6ed-07bf-4616-80f0-47546850f654"


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if (not isinstance(value, str) or not value.strip() or len(value) > 16384
            or any(ord(c) < 32 for c in value)):
        raise CatalogError(f"Required deployment setting {name} is missing or invalid")
    return value


def identifier(env: Mapping[str, str], name: str) -> str:
    try:
        return str(UUID(required(env, name)))
    except ValueError:
        raise CatalogError(f"Deployment setting {name} must be a UUID") from None


def endpoint(env: Mapping[str, str], name: str, suffix: str) -> str:
    value = required(env, name).rstrip("/")
    _https_origin(value)
    if not urlsplit(value).hostname.endswith(suffix):
        raise CatalogError(f"Deployment setting {name} has an unapproved host")
    return value


def platform_callers(env: Mapping[str, str]) -> frozenset[str]:
    try:
        callers = json.loads(required(env, "CONTOSO_PLATFORM_CALLER_IDS"))
    except json.JSONDecodeError:
        raise CatalogError("Platform caller grants must be a JSON list") from None
    if (not isinstance(callers, list) or not 1 <= len(callers) <= 10
            or any(not isinstance(c, str) or not c.strip() or len(c) > 8192
                   or any(ord(ch) < 32 for ch in c) for c in callers)
            or len(set(callers)) != len(callers)):
        raise CatalogError("Explicit unique platform caller grants are required")
    return frozenset(callers)


def identity_link_transport(env: Mapping[str, str]) -> tuple[Protocol, str]:
    """Optional diagnostic SDK link only; business publication requires native A2A."""
    setting = "CONTOSO_IDENTITY_LINK_TRANSPORT"
    selected = env.get(setting, "a2a")
    if selected == "sdk":
        target = endpoint(env, "CONTOSO_KNOWLEDGE_RESPONSES_ENDPOINT", ".services.ai.azure.com")
        if (urlsplit(target).query != "api-version=v1"
                or not re.fullmatch(
                    r"/api/projects/[A-Za-z0-9][A-Za-z0-9._-]*/agents/"
                    r"[A-Za-z0-9][A-Za-z0-9._-]*/endpoint/protocols/openai/responses",
                    urlsplit(target).path,
                )):
            raise CatalogError("The diagnostic SDK link requires the native Hosted Responses endpoint")
        return Protocol.FOUNDRY_RESPONSES, target
    if selected == "a2a":
        return Protocol.A2A, endpoint(env, "CONTOSO_KNOWLEDGE_A2A_ENDPOINT", ".services.ai.azure.com")
    raise CatalogError(f"Deployment setting {setting} must select sdk or a2a explicitly")


@dataclass(frozen=True)
class HarnessRouterSettings:
    """Today's single STUDIO Harness route; no Standard, Knowledge, or SDK alternative."""

    tenant_id: str
    gateway_url: str
    gateway_api_id: str
    harness_endpoint: str
    callers: frozenset[str]

    @classmethod
    def from_environment(cls, env: Mapping[str, str]):
        return cls(
            tenant_id=identifier(env, "CONTOSO_ENTRA_TENANT_ID"),
            gateway_url=endpoint(env, "CONTOSO_GATEWAY_URL", ".azurewebsites.net"),
            gateway_api_id=identifier(env, "CONTOSO_ENTRA_API_CLIENT_ID"),
            harness_endpoint=endpoint(env, "CONTOSO_STUDIO_HARNESS_ENDPOINT", ".powerplatform.com"),
            callers=platform_callers(env),
        )

    def catalog(self) -> Catalog:
        entry = CatalogEntry(
            "studio-copilot-harness", Capability.SUPPLIER_DELIVERY_TERMS,
            STUDIOVariant.COPILOT_HARNESS, self.harness_endpoint, Protocol.A2A,
            AuthMetadata(
                AuthScheme.ENTRA_BEARER, self.tenant_id, HARNESS_AUDIENCE,
                frozenset({"https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"}),
            ),
            (SourceMetadata(
                "CONTOSO-DEMO-SUPPLIER-001", "studio-copilot-harness", "1",
                "Supplier partial-delivery terms",
                "Synthetic demonstration data", "contoso-demo/knowledge/supplier-delivery-terms.md",
                frozenset({
                    "Fictional proposal", "Buyer acceptance conditions",
                    "Recommended next step", "Evidence limits",
                }),
            ),),
        )
        return Catalog(
            (entry,),
            tuple(CallerGrant(caller, frozenset({Capability.SUPPLIER_DELIVERY_TERMS}))
                  for caller in self.callers),
            frozenset({_https_origin(entry.endpoint)}),
        )


@dataclass(frozen=True)
class RouterSettings:
    tenant_id: str
    environment_id: str
    gateway_url: str
    gateway_api_id: str
    standard_endpoint: str
    standard_schema: str
    harness_endpoint: str
    knowledge_endpoint: str
    callers: frozenset[str]

    @classmethod
    def from_environment(cls, env: Mapping[str, str]):
        environment_id = identifier(env, "CONTOSO_STUDIO_ENVIRONMENT_ID")
        standard_schema = required(env, "CONTOSO_STUDIO_STANDARD_SCHEMA")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", standard_schema):
            raise CatalogError("The configured Standard STUDIO schema is invalid")
        if env.get("CONTOSO_KNOWLEDGE_TRANSPORT", "a2a") != "a2a":
            raise CatalogError("Business knowledge transport requires native A2A; SDK is diagnostic only")
        return cls(
            tenant_id=identifier(env, "CONTOSO_ENTRA_TENANT_ID"),
            environment_id=environment_id,
            gateway_url=endpoint(env, "CONTOSO_GATEWAY_URL", ".azurewebsites.net"),
            gateway_api_id=identifier(env, "CONTOSO_ENTRA_API_CLIENT_ID"),
            standard_endpoint=standard_catalog_endpoint(
                environment_id=environment_id, agent_identifier=standard_schema,
            ),
            standard_schema=standard_schema,
            harness_endpoint=endpoint(env, "CONTOSO_STUDIO_HARNESS_ENDPOINT", ".powerplatform.com"),
            knowledge_endpoint=endpoint(env, "CONTOSO_KNOWLEDGE_A2A_ENDPOINT", ".services.ai.azure.com"),
            callers=platform_callers(env),
        )

    def catalog(self) -> Catalog:
        power_platform = "https://api.powerplatform.com"
        entries = [
            CatalogEntry(
                provider_id="knowledge-demo",
                capability=Capability.KNOWLEDGE_KNOWLEDGE,
                variant=None,
                endpoint=self.knowledge_endpoint,
                protocol=Protocol.A2A,
                auth=AuthMetadata(
                    AuthScheme.ENTRA_BEARER, self.tenant_id, "https://ai.azure.com",
                    frozenset({"https://ai.azure.com/.default"}),
                ),
                sources=(
                    SourceMetadata(
                        "CONTOSO-DEMO-KNOWLEDGE-001", "knowledge-demo", "1",
                        "Supplier–buyer reusable transport-container coordination",
                        "Synthetic demonstration data", "contoso-demo/knowledge/knowledge-returnables.md",
                        frozenset({
                            "Initial problem", "Approved intervention", "Recommended next step",
                            "Evidence limits",
                        }),
                    ),
                ),
            ),
        ]
        for variant, target, scope, audience in (
            (STUDIOVariant.STANDARD, self.standard_endpoint,
             "https://api.powerplatform.com/CopilotStudio.Copilots.Invoke", power_platform),
            (STUDIOVariant.COPILOT_HARNESS, self.harness_endpoint,
             "https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A", HARNESS_AUDIENCE),
        ):
            provider = f"studio-{variant.value}"
            entries.append(CatalogEntry(
                provider, Capability.SUPPLIER_DELIVERY_TERMS, variant, target,
                Protocol.SDK_D2E if variant == STUDIOVariant.STANDARD else Protocol.A2A,
                AuthMetadata(
                    AuthScheme.ENTRA_BEARER, self.tenant_id, audience, frozenset({scope}),
                ),
                (SourceMetadata(
                    "CONTOSO-DEMO-SUPPLIER-001", provider, "1",
                    "Supplier partial-delivery terms",
                    "Synthetic demonstration data", "contoso-demo/knowledge/supplier-delivery-terms.md",
                    frozenset({
                        "Fictional proposal", "Buyer acceptance conditions",
                        "Recommended next step", "Evidence limits",
                    }),
                ),),
            ))
        return Catalog(
            tuple(entries),
            tuple(CallerGrant(caller, frozenset(Capability)) for caller in self.callers),
            frozenset({_https_origin(entry.endpoint) for entry in entries}),
        )


def demo_router(*, capability: Capability | None = None) -> DemoQuestionRouter:
    path = Path(__file__).resolve().parents[1] / "contoso-demo" / "scenarios.json"
    with path.open(encoding="utf-8") as source:
        dataset = json.load(source)
    return DemoQuestionRouter({
        scenario["question"]: scenario["expected_capability"]
        for scenario in dataset["scenarios"]
        if capability is None or scenario["expected_capability"] == capability.value
    })
