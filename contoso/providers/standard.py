"""Standard STUDIO transport using microsoft-agents-copilotstudio-client v1.9.0."""

import asyncio
from collections.abc import AsyncIterator, Callable
import re
from typing import Protocol as TypingProtocol
from urllib.parse import urlsplit

from contoso.router.core import CatalogEntry, STUDIOVariant, Protocol
from .common import (
    ConfigurationError, ProtocolError, ProviderTimeout, RawReply,
    ResponseTooLarge, TransportError, reply, validate_call, validate_limits,
    validate_origins,
)

STANDARD_SCOPE = "https://api.powerplatform.com/CopilotStudio.Copilots.Invoke"


class Conversation(TypingProtocol):
    id: str | None


class Activity(TypingProtocol):
    type: str
    text: str | None
    conversation: Conversation | None


class Client(TypingProtocol):
    def start_conversation(self, emit_start_conversation_event: bool) -> AsyncIterator[Activity]: ...
    def ask_question(self, question: str, conversation_id: str) -> AsyncIterator[Activity]: ...


ClientFactory = Callable[[str, str, str], Client]


def _validate_identity(environment_id: str, agent_identifier: str) -> None:
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
           for value in (environment_id, agent_identifier)):
        raise ConfigurationError("Standard environment and agent configuration is invalid")


def standard_catalog_endpoint(*, environment_id: str, agent_identifier: str) -> str:
    """Return the SDK 1.9.0 initial discovery URL, without tokens or network.

    Use this exact value for CatalogEntry.endpoint and approve its HTTPS origin.
    The SDK performs discovery and selects conversation/turn URLs itself; this
    catalog value is not passed as a direct_connect_url override.
    The version-pinned SDK discovery helper avoids copying environment/URL rules.
    """
    _validate_identity(environment_id, agent_identifier)
    try:
        from microsoft_agents.copilotstudio.client import ConnectionSettings
        from microsoft_agents.copilotstudio.client._conversation_endpoint_discovery import get_discovery_url
    except ImportError:
        raise ConfigurationError("Standard Copilot SDK dependency is unavailable") from None
    try:
        settings = ConnectionSettings(
            environment_id=environment_id, agent_identifier=agent_identifier,
            enable_diagnostics=False,
        )
        endpoint = get_discovery_url(settings)
    except (ValueError, TypeError, RuntimeError, OSError):
        raise ConfigurationError("Standard SDK endpoint configuration is invalid") from None
    if not isinstance(endpoint, str) or not endpoint:
        raise ConfigurationError("Standard SDK endpoint configuration is invalid")
    return endpoint


def _client(environment_id: str, agent_identifier: str, token: str) -> Client:
    try:
        from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient
    except ImportError:
        raise ConfigurationError("Standard Copilot SDK dependency is unavailable") from None
    settings = ConnectionSettings(
        environment_id=environment_id, agent_identifier=agent_identifier,
        enable_diagnostics=False,
    )
    return CopilotClient(settings, token)


class StandardSDKAdapter:
    """Factory arguments are environment ID, agent identifier, in-memory token.

    Injected clients must disable diagnostics and must not acquire/cache tokens.
    SDK messages have no task state, so RawReply.state is None.
    """

    def __init__(
        self, *, approved_origins: frozenset[str], environment_id: str,
        agent_identifier: str, overall_timeout: float = 60,
        max_response_bytes: int = 1_048_576, client_factory: ClientFactory | None = None,
    ):
        validate_limits(overall_timeout, max_response_bytes=max_response_bytes)
        _validate_identity(environment_id, agent_identifier)
        if client_factory is not None and not callable(client_factory):
            raise ConfigurationError("Standard client factory is invalid")
        self.approved_origins = validate_origins(approved_origins)
        self.environment_id = environment_id
        self.agent_identifier = agent_identifier
        self.overall_timeout = overall_timeout
        self.max_response_bytes = max_response_bytes
        self.client_factory = client_factory

    async def answer(self, entry: CatalogEntry, question: str, token: str) -> RawReply:
        validate_call(entry, question, token, self.approved_origins,
                      Protocol.SDK_D2E, STUDIOVariant.STANDARD, STANDARD_SCOPE)
        endpoint = urlsplit(entry.endpoint)
        if (endpoint.hostname is None or endpoint.port not in (None, 443)
                or (endpoint.hostname != "api.powerplatform.com"
                    and not endpoint.hostname.endswith(".environment.api.powerplatform.com"))):
            raise ConfigurationError("Standard endpoint configuration is invalid")
        if self.client_factory is None and entry.endpoint != standard_catalog_endpoint(
            environment_id=self.environment_id, agent_identifier=self.agent_identifier,
        ):
            raise ConfigurationError("Standard catalog endpoint does not match SDK configuration")
        transport_errors = (OSError, RuntimeError, ValueError, TypeError)
        if self.client_factory is None:
            try:
                from aiohttp import ClientError
            except ImportError:
                raise ConfigurationError("Standard Copilot SDK dependency is unavailable") from None
            transport_errors += (ClientError,)
        try:
            async with asyncio.timeout(self.overall_timeout):
                client = (self.client_factory or _client)(
                    self.environment_id, self.agent_identifier, token,
                )
                conversation_id = None
                response_bytes = 0

                def measure(value: str) -> None:
                    nonlocal response_bytes
                    try:
                        response_bytes += len(value.encode("utf-8"))
                    except UnicodeError:
                        raise ProtocolError("Standard response text encoding is malformed") from None
                    if response_bytes > self.max_response_bytes:
                        raise ResponseTooLarge("Standard response exceeds the byte limit")

                async for activity in client.start_conversation(True):
                    text = getattr(activity, "text", None)
                    if isinstance(text, str):
                        measure(text)
                    conversation = getattr(activity, "conversation", None)
                    candidate = getattr(conversation, "id", None)
                    if candidate is not None:
                        if not isinstance(candidate, str) or not candidate.strip():
                            raise ProtocolError("Standard conversation ID is malformed")
                        measure(candidate)
                        if conversation_id is not None and conversation_id != candidate:
                            raise ProtocolError("Standard conversation ID changed")
                        conversation_id = candidate
                if conversation_id is None:
                    raise ProtocolError("Standard response has no conversation ID")
                texts = []
                async for activity in client.ask_question(question, conversation_id):
                    conversation = getattr(activity, "conversation", None)
                    candidate = getattr(conversation, "id", None)
                    if candidate is not None and candidate != conversation_id:
                        raise ProtocolError("Standard answer conversation ID changed")
                    text = getattr(activity, "text", None)
                    if isinstance(text, str):
                        measure(text)
                    if getattr(activity, "type", None) == "message":
                        if text is not None and not isinstance(text, str):
                            raise ProtocolError("Standard message text is malformed")
                        if text is not None and text.strip():
                            texts.append(text)
                if not texts:
                    raise ProtocolError("Standard response contains no message text")
                text = "\n".join(texts)
                if len(text.encode("utf-8")) > self.max_response_bytes:
                    raise ResponseTooLarge("Standard response exceeds the byte limit")
        except TimeoutError:
            raise ProviderTimeout("Standard request timed out") from None
        except transport_errors:
            raise TransportError("Standard request failed") from None
        return reply(
            entry, text, channel="copilotstudio-sdk", state=None,
            environment_id=self.environment_id, agent_identifier=self.agent_identifier,
            conversation_id=conversation_id,
        )
