"""Stateless routing for the bounded demo catalog and real provider transports."""

from collections.abc import Awaitable, Callable, Mapping
import asyncio
from dataclasses import dataclass, field
import logging
import math
import time
from typing import Protocol as TypingProtocol, TypedDict

from langgraph.graph import END, START, StateGraph

from contoso.providers import AttributionError, ProviderError, RawReply, extract_citations
from contoso.providers.citations import log_attribution_rejection
from contoso.providers.diagnostics import log_stage
from .core import (
    Capability, Catalog, CatalogEntry, CatalogError, STUDIOVariant,
    RouterResponse, ProviderResponse, ResponseStatus, RouteDecision, RouteStatus,
)


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopedToken:
    token: str = field(repr=False)
    tenant_id: str
    audience: str
    scopes: frozenset[str]
    expires_at: float

    def __post_init__(self):
        if (not isinstance(self.token, str) or not self.token or len(self.token) > 16384
                or any(c.isspace() or ord(c) < 32 for c in self.token)):
            raise CatalogError("The configured provider token is invalid")
        if not isinstance(self.scopes, (set, frozenset, tuple, list)):
            raise CatalogError("Provider scopes must be a collection")
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        if (not isinstance(self.tenant_id, str) or not self.tenant_id
                or not isinstance(self.audience, str) or not self.audience or not self.scopes
                or any(not isinstance(scope, str) or not scope
                       or any(c.isspace() or ord(c) < 32 for c in scope)
                       for scope in self.scopes)
                or type(self.expires_at) not in (int, float)
                or not math.isfinite(self.expires_at)):
            raise CatalogError("Provider token metadata is required")

    def require(self, entry: CatalogEntry, now: float) -> None:
        if (self.tenant_id != entry.auth.tenant_id
                or self.audience != entry.auth.audience
                or not entry.auth.scopes.issubset(self.scopes)
                or self.expires_at <= now):
            raise CatalogError("Provider token does not match the authorized route")


class ProviderAdapter(TypingProtocol):
    async def answer(self, entry: CatalogEntry, question: str, token: str) -> RawReply: ...


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    answer: str
    result: RouterResponse | None
    path: tuple[str, ...]

    def to_dict(self) -> dict:
        response = self.result.response if self.result is not None else None
        sources = self.result.sources if self.result is not None else ()
        return {
            "status": self.status,
            "answer": self.answer,
            "provider_id": response.provider_id if response is not None else None,
            "studio_variant": response.variant.value if response and response.variant else None,
            "sources": [
                {
                    "source_id": source.source_id, "provider_id": source.provider_id,
                    "version": source.version, "title": source.title,
                    "classification": source.classification,
                }
                for source in sources
            ],
            "citations": [
                {"source_id": citation.source_id, "version": citation.version,
                 "section": citation.section}
                for citation in response.citations
            ] if response is not None else [],
            "execution_path": list(self.path),
        }


class DemoQuestionRouter:
    """Only the registered demo questions are supported, not general intent inference."""

    def __init__(self, questions: Mapping[str, str | None]):
        if not isinstance(questions, Mapping) or not questions:
            raise CatalogError("Registered demo questions are required")
        self._questions: dict[str, str | None] = {}
        for question, capability in questions.items():
            normalized = self.normalize(question)
            if not normalized or normalized in self._questions:
                raise CatalogError("Demo questions must be unique and nonempty")
            if capability is not None:
                try:
                    Capability(capability)
                except (ValueError, TypeError):
                    raise CatalogError("Unknown demo capability") from None
            self._questions[normalized] = capability

    @staticmethod
    def normalize(question: str) -> str:
        if not isinstance(question, str):
            raise CatalogError("Question must be text")
        return " ".join(question.split()).casefold()

    async def __call__(self, question: str) -> str | None:
        return self._questions.get(self.normalize(question))


class _State(TypedDict, total=False):
    question: str
    caller_id: str
    variant: str | None
    capability: str | None
    decision: RouteDecision
    result: RuntimeResult


def validate_runtime_request(question: str, variant: str | None) -> None:
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise CatalogError("Question must contain at most 2000 characters and 8000 bytes")
    try:
        encoded = question.encode("utf-8")
    except UnicodeEncodeError:
        raise CatalogError("Question must be valid Unicode") from None
    if len(encoded) > 8000:
        raise CatalogError("Question must contain at most 2000 characters and 8000 bytes")
    if variant is not None:
        try:
            STUDIOVariant(variant)
        except (TypeError, ValueError):
            raise CatalogError("Unknown STUDIO variant") from None


class RouterRuntime:
    def __init__(
        self,
        catalog: Catalog,
        classifier: Callable[[str], Awaitable[str | None]],
        providers: Mapping[str, ProviderAdapter],
        token_provider: Callable[[CatalogEntry], Awaitable[ScopedToken]],
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.catalog = catalog
        self.classifier = classifier
        self.providers = dict(providers)
        self.token_provider = token_provider
        self.clock = clock
        graph = StateGraph(_State)
        graph.add_node("classify", self._classify)
        graph.add_node("route", self._route)
        graph.add_node("invoke", self._invoke)
        graph.add_edge(START, "classify")
        graph.add_edge("classify", "route")
        graph.add_conditional_edges("route", self._next, {"invoke": "invoke", "done": END})
        graph.add_edge("invoke", END)
        self.graph = graph.compile()

    async def _classify(self, state: _State) -> dict:
        capability = await self.classifier(state["question"])
        if capability is not None:
            try:
                Capability(capability)
            except (ValueError, TypeError):
                raise CatalogError("Classifier returned an unsupported capability") from None
        return {"capability": capability}

    async def _route(self, state: _State) -> dict:
        capability = state["capability"]
        decision = self.catalog.lookup(
            state["caller_id"],
            capability if capability is not None else "unregistered-capability",
            variant=state["variant"] if capability == Capability.SUPPLIER_DELIVERY_TERMS else None,
        )
        update: dict = {"decision": decision}
        if decision.status == RouteStatus.NO_MATCH:
            update["result"] = RuntimeResult(
                RouteStatus.NO_MATCH.value,
                "No approved agent matches this request. No knowledge provider was called.",
                None,
                ("Authenticated caller", "Demo capability catalog", "No matching capability"),
            )
        return update

    @staticmethod
    def _next(state: _State) -> str:
        return "invoke" if state["decision"].status == RouteStatus.MATCH else "done"

    async def _invoke(self, state: _State) -> dict:
        entry = state["decision"].entry
        if entry is None:
            raise CatalogError("Matched route has no provider")
        provider = self.providers.get(entry.provider_id)
        if provider is None:
            raise CatalogError("The configured provider transport is unavailable")
        stage = "provider_token"
        try:
            log_stage(_logger, entry, stage, "start")
            token = await self.token_provider(entry)
            if not isinstance(token, ScopedToken):
                raise CatalogError("The token broker returned invalid credentials")
            token.require(entry, self.clock())
            log_stage(_logger, entry, stage, "complete")
            stage = "provider_transport"
            log_stage(_logger, entry, stage, "start")
            reply = await provider.answer(entry, state["question"], token.token)
            log_stage(_logger, entry, stage, "complete")
            stage = "source_attribution"
            log_stage(_logger, entry, stage, "start")
            citations = extract_citations(entry, reply)
            response = ProviderResponse(
                entry.provider_id, entry.capability, entry.variant,
                ResponseStatus.ANSWERED, reply.text, citations,
            )
            result = self.catalog.validate_response(
                state["caller_id"], entry.capability, response, variant=entry.variant,
            )
            log_stage(_logger, entry, stage, "complete")
        except (CatalogError, ProviderError) as error:
            if stage == "source_attribution" and isinstance(error, AttributionError):
                log_attribution_rejection(_logger, entry, reply, error)
            log_stage(_logger, entry, stage, "failed", error=error)
            raise
        except asyncio.CancelledError as error:
            log_stage(_logger, entry, stage, "cancelled", error=error)
            raise
        return {
            "result": RuntimeResult(
                response.status.value, reply.text, result,
                ("Authenticated caller", "Demo capability catalog",
                 f"{entry.provider_id} via {entry.protocol.value}", "Verified source attribution"),
            )
        }

    async def run(
        self, question: str, *, caller_id: str, variant: str | None = None,
    ) -> RuntimeResult:
        """caller_id is supplied only by the verified transport adapter."""
        self.catalog.require_caller(caller_id)
        validate_runtime_request(question, variant)
        state = await self.graph.ainvoke(
            {"question": question, "caller_id": caller_id, "variant": variant},
        )
        return state["result"]
