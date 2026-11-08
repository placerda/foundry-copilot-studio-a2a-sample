"""Authenticated diagnostic link. Never serves knowledge or delegated tokens."""

import asyncio
from threading import BoundedSemaphore
import time

from azure.core.exceptions import AzureError

from contoso.bounded import run_bounded_sync
from contoso.identity_probe import build_json_probe_host, parse_probe_reply
from contoso.router.core import AccessDenied, _https_origin
from contoso.router.hosted import current_platform_context, parse_input, require_platform_caller
from contoso.providers import A2ATransport, ProviderTimeout, TransportError


class IdentityLinkProbe:
    def __init__(
        self, credential, *, endpoint, allowed_callers,
        context_factory=current_platform_context, transport=None,
    ):
        if not allowed_callers:
            raise ValueError("An identity link requires approved platform callers")
        self.credential = credential
        self.allowed_callers = frozenset(allowed_callers)
        self.context_factory = context_factory
        self._slot = BoundedSemaphore(1)
        self.transport = transport if transport is not None else A2ATransport(
            endpoint=endpoint, approved_origins=frozenset({_https_origin(endpoint)}),
            overall_timeout=40,
            platform_headers_factory=lambda: context_factory().platform_headers(),
        )

    def _token(self):
        try:
            token = self.credential.get_token("https://ai.azure.com/.default")
        except AzureError:
            raise TransportError("The diagnostic service token is unavailable") from None
        if not token.token or token.expires_on <= time.time() + 5:
            raise TransportError("The diagnostic service token is expired")
        return token

    async def _link(self):
        token = await run_bounded_sync(self._slot, self._token)
        text = await self.transport.invoke("CONTOSO identity probe", token.token)
        try:
            return parse_probe_reply(text)
        except ValueError:
            raise TransportError("The downstream service is not an identity-bootstrap deployment") from None

    async def answer(self, request, cancellation):
        platform = self.context_factory()
        caller = require_platform_caller(platform)
        if caller not in self.allowed_callers:
            raise AccessDenied("This platform caller is not approved for diagnostic linking")
        question, variant = parse_input(request)
        if question != "CONTOSO identity link probe" or variant is not None:
            raise AccessDenied("This diagnostic version cannot serve business requests")
        if cancellation.is_set():
            raise asyncio.CancelledError
        work = asyncio.create_task(self._link())
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (work, cancelled), timeout=45, return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            if work not in done:
                raise ProviderTimeout("The authenticated diagnostic link exceeded its time budget")
            downstream = await work
            return {
                "status": "identity-link-bootstrap",
                "platform_caller_id": caller,
                "platform_call_id": platform.platform_headers()["x-agent-foundry-call-id"],
                "downstream": downstream,
                "knowledge_access": False,
            }
        finally:
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

    def build_host(self):
        async def resolve(request, cancellation):
            return await self.answer(request, cancellation)

        return build_json_probe_host(resolve)
