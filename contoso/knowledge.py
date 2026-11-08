"""Extractive Hosted provider for one approved, version-pinned demo document."""

import asyncio
from collections.abc import Callable, Mapping
import hashlib
import re
import threading
from typing import Protocol

from azure.core import MatchConditions
from azure.core.exceptions import AzureError

from contoso.bounded import run_bounded_sync
from contoso.router.core import AccessDenied, CatalogError
from contoso.router.hosted import (
    PlatformContext, current_platform_context, parse_input, require_platform_caller,
)
from contoso.router.runtime import DemoQuestionRouter
from contoso.providers import ProviderTimeout, TransportError


QUESTION = (
    "What process can a supplier and buyer use to coordinate reusable "
    "transport-container returns?"
)
MAX_SOURCE_BYTES = 32768
ATTRIBUTION = (
    '\nSource: CONTOSO-DEMO-KNOWLEDGE-001, Version 1, Sections: '
    '"Approved intervention" and "Evidence limits".\n'
)


class SourceBlob(Protocol):
    def get_blob_properties(self): ...

    def download_blob(self, **kwargs): ...


class KnowledgeHost:
    def __init__(
        self,
        blob: SourceBlob,
        *,
        source_sha256: str,
        allowed_callers: frozenset[str],
        context_factory: Callable[[], PlatformContext] = current_platform_context,
    ):
        if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
            raise CatalogError("A pinned source SHA-256 is required")
        if (not allowed_callers
                or any(not isinstance(c, str) or not c.strip() for c in allowed_callers)):
            raise CatalogError("Explicit Foundry caller grants are required")
        self.blob = blob
        self.source_sha256 = source_sha256
        self.allowed_callers = frozenset(allowed_callers)
        self.context_factory = context_factory
        self._slot = threading.BoundedSemaphore(1)

    def _read_source(self) -> str:
        try:
            properties = self.blob.get_blob_properties()
            if (type(properties.size) is not int
                    or not 0 < properties.size <= MAX_SOURCE_BYTES):
                raise CatalogError("The approved source has an invalid size")
            content = self.blob.download_blob(
                etag=properties.etag,
                match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            ).readall()
            if (not isinstance(content, bytes) or len(content) != properties.size
                    or hashlib.sha256(content).hexdigest() != self.source_sha256):
                raise CatalogError("The source does not match its approved version")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                raise CatalogError("The approved source is not UTF-8") from None
            if ("Source ID: CONTOSO-DEMO-KNOWLEDGE-001" not in text
                    or "Version: 1" not in text
                    or "## Approved intervention" not in text
                    or "## Evidence limits" not in text):
                raise CatalogError("The approved source metadata is inconsistent")
            return text + ATTRIBUTION
        except AzureError:
            raise TransportError("The private approved source is unavailable") from None

    async def answer(self, request: Mapping, cancellation: asyncio.Event) -> str:
        caller = require_platform_caller(self.context_factory())
        if caller not in self.allowed_callers:
            raise AccessDenied("This Foundry caller is not approved for the knowledge source")
        if cancellation.is_set():
            raise asyncio.CancelledError
        question, variant = parse_input(request)
        if (variant is not None
                or DemoQuestionRouter.normalize(question) != DemoQuestionRouter.normalize(QUESTION)):
            raise CatalogError("The knowledge provider supports only the registered demo question")
        read = asyncio.create_task(run_bounded_sync(self._slot, self._read_source))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (read, cancelled), timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            if read not in done:
                raise ProviderTimeout("The private source read exceeded its time budget")
            return await read
        finally:
            read.cancel()
            cancelled.cancel()
            await asyncio.gather(read, cancelled, return_exceptions=True)

    def build_host(self):
        from azure.ai.agentserver.responses import (
            CreateResponse, InMemoryResponseProvider, ResponseContext,
            ResponsesAgentServerHost, ResponsesServerOptions, TextResponse,
        )

        host = ResponsesAgentServerHost(
            store=InMemoryResponseProvider(),
            options=ResponsesServerOptions(
                default_fetch_history_count=1,
                resilient_background=False,
                steerable_conversations=False,
            ),
        )

        @host.response_handler
        async def handler(
            request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event,
        ):
            text = await self.answer(request, cancellation_signal)
            return TextResponse(context, request, text=text)

        return host
