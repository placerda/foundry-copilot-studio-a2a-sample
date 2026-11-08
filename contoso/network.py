"""The Gateway invokes actual Hosted Routers with its own managed identity."""

import asyncio
import json
import threading
from collections.abc import Callable

from azure.core.exceptions import AzureError
from azure.ai.projects import AIProjectClient
from openai import OpenAIError

from contoso.bounded import run_bounded_sync
from contoso.router.core import (
    Capability, Catalog, CatalogError, Citation, ProviderResponse, ResponseStatus, RouteStatus,
    _https_origin,
)
from contoso.providers import A2ATransport, ProtocolError, ProviderTimeout, TransportError
from contoso.token_client import Credential


class FoundryNetworkClient:
    def __init__(
        self,
        credential: Credential,
        *,
        project_endpoint: str,
        sdk_agent_name: str,
        a2a_endpoint: str,
        catalog: Catalog,
        catalog_caller_id: str,
        project_factory: Callable = AIProjectClient,
        a2a_factory: Callable = A2ATransport,
    ):
        project_origin = _https_origin(project_endpoint)
        if _https_origin(a2a_endpoint) != project_origin:
            raise CatalogError("Both Hosted Routers must belong to the selected Foundry resource")
        if (not isinstance(sdk_agent_name, str) or not sdk_agent_name
                or len(sdk_agent_name) > 128
                or any(not (ch.isalnum() or ch in "-_") for ch in sdk_agent_name)):
            raise CatalogError("A fixed SDK Router name is required")
        catalog.require_caller(catalog_caller_id)
        self.credential = credential
        self.project_endpoint = project_endpoint
        self.sdk_agent_name = sdk_agent_name
        self.catalog = catalog
        self.catalog_caller_id = catalog_caller_id
        self.project_factory = project_factory
        self.a2a = a2a_factory(
            endpoint=a2a_endpoint,
            approved_origins=frozenset({project_origin}),
            overall_timeout=70,
        )
        self._slot = threading.BoundedSemaphore(1)

    def _sdk(self, message: str) -> str:
        try:
            with self.project_factory(
                endpoint=self.project_endpoint,
                credential=self.credential,
                connection_timeout=5, read_timeout=65, retry_total=0,
            ) as project:
                with project.get_openai_client(
                    agent_name=self.sdk_agent_name, timeout=70, max_retries=0,
                ) as client:
                    response = client.responses.create(input=message, store=False)
                    return response.output_text
        except (AzureError, OpenAIError):
            raise TransportError("The selected SDK Router is unavailable") from None

    def _a2a_token(self) -> str:
        try:
            return self.credential.get_token("https://ai.azure.com/.default").token
        except AzureError:
            raise TransportError("The Gateway service credential is unavailable") from None

    def _validate(self, text: str) -> dict:
        from contoso.gateway import validate_runtime_result

        if not isinstance(text, str) or len(text.encode("utf-8")) > 1048576:
            raise ProtocolError("The Router returned an invalid or oversized result")
        try:
            payload = json.loads(text, object_pairs_hook=_unique_fields)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProtocolError("The Router did not return a CONTOSO result") from None
        payload = validate_runtime_result(payload)
        if payload["status"] == RouteStatus.NO_MATCH.value:
            return payload
        capability = (Capability.KNOWLEDGE_KNOWLEDGE if payload["provider_id"] == "knowledge-demo"
                      else Capability.SUPPLIER_DELIVERY_TERMS)
        decision = self.catalog.lookup(
            self.catalog_caller_id, capability, variant=payload["studio_variant"],
        )
        entry = decision.entry
        if entry is None or entry.provider_id != payload["provider_id"]:
            raise ProtocolError("The Router selected an unapproved provider")
        response = ProviderResponse(
            entry.provider_id, entry.capability, entry.variant,
            ResponseStatus(payload["status"]), payload["answer"],
            tuple(Citation(c["source_id"], entry.provider_id, c["version"], c["section"])
                  for c in payload["citations"]),
        )
        verified = self.catalog.validate_response(
            self.catalog_caller_id, capability, response, variant=entry.variant,
        )
        expected = [
            {
                "source_id": source.source_id, "provider_id": source.provider_id,
                "version": source.version, "title": source.title,
                "classification": source.classification,
            }
            for source in verified.sources
        ]
        if payload["sources"] != expected:
            raise ProtocolError("The Router source metadata differs from the approved catalog")
        return payload

    async def invoke(self, question: str, studio_variant: str, integration: str) -> dict:
        if (not isinstance(question, str) or not question.strip() or len(question) > 2000
                or len(question.encode("utf-8")) > 8000
                or studio_variant not in ("standard", "copilot-harness")
                or integration not in ("sdk", "a2a")):
            raise CatalogError("A valid bounded CONTOSO question and explicit selections are required")
        message = json.dumps({"question": question, "studio_variant": studio_variant}, ensure_ascii=True)

        async def invoke():
            if integration == "sdk":
                return await run_bounded_sync(self._slot, lambda: self._sdk(message))
            token = await run_bounded_sync(self._slot, self._a2a_token)
            return await self.a2a.invoke(message, token)

        try:
            text = await asyncio.wait_for(invoke(), timeout=75)
        except TimeoutError:
            raise ProviderTimeout("The selected Router exceeded the request budget") from None
        return self._validate(text)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("The Router returned duplicate result fields")
        result[key] = value
    return result
