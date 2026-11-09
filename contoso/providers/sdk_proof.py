"""Isolated operator proof; never used by the shared-delegated Router."""

import asyncio
import hashlib
import os
import re
from urllib.parse import urlsplit
from uuid import UUID

import jwt

from contoso.identity_probe import build_json_probe_host
from contoso.configuration import platform_callers
from contoso.router.core import AccessDenied
from contoso.router.hosted import current_platform_context, parse_input, require_platform_caller


RESOURCE = "https://api.powerplatform.com"
RESOURCE_APP = "8578e004-a5c6-46e7-913e-12f58912df43"
ROLE = "CopilotStudio.Copilots.Invoke"
QUESTION = (
    "We have an order for 500 units due Friday. We propose delivering 300 units Friday "
    "and 200 units Monday. Does this partial delivery satisfy the buyer's terms?"
)


def issued_token_metadata(token, env=None):
    env = os.environ if env is None else env
    tenant, principal_id, client_id, _ = proof_bindings(env)
    if not isinstance(token, str) or not 1 <= len(token) <= 32768:
        raise ValueError("Invalid credential")
    # This client does not own Power Platform tokens. These diagnostic claims
    # can stop a mismatched probe, but only Studio can authorize the issued token.
    claims = jwt.decode(
        token,
        audience=[RESOURCE, RESOURCE + "/", RESOURCE_APP],
        issuer=[
            f"https://sts.windows.net/{tenant}/",
            f"https://login.microsoftonline.com/{tenant}/v2.0",
        ],
        options={
            "verify_signature": False, "verify_exp": True,
            "verify_aud": True, "verify_iss": True,
            "require": ["exp", "iat", "aud", "iss", "tid", "oid"],
        },
    )
    if claims["tid"] != tenant:
        raise ValueError("Unexpected tenant")
    principal = str(UUID(claims["oid"]))
    client = str(UUID(claims.get("azp") or claims.get("appid", "")))
    roles = claims.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise ValueError("Invalid roles")
    return {
        "tenant_id": tenant, "principal_id": principal, "client_id": client,
        "audience": claims["aud"], "signature_validated": False,
        "metadata_source": "issued-token-diagnostic", "resource_authorized": False,
        "sdk_identity_matched": principal == principal_id and client == client_id,
        "required_role_present": ROLE in roles,
        "application_only": not bool(claims.get("scp")),
    }


def configuration(env):
    tenant = str(UUID(env["CONTOSO_ENTRA_TENANT_ID"]))
    environment = str(UUID(env["CONTOSO_STUDIO_ENVIRONMENT_ID"]))
    endpoint = urlsplit(env["CONTOSO_STUDIO_HARNESS_ENDPOINT"])
    prefix = f"/copilotstudio-a2a/tenants/{tenant}/environments/{environment}/agents/"
    schema = endpoint.path.removeprefix(prefix)
    if (
        endpoint.scheme != "https" or endpoint.netloc != "api.powerplatform.com"
        or endpoint.query or endpoint.fragment or not endpoint.path.startswith(prefix)
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}", schema)
    ):
        raise ValueError("Invalid fixed Studio target")
    callers = platform_callers(env)
    return environment, schema, callers


def proof_bindings(env):
    environment, schema, _ = configuration(env)
    tenant = str(UUID(env["CONTOSO_ENTRA_TENANT_ID"]))
    principal = str(UUID(env["CONTOSO_SDK_EXPECTED_PRINCIPAL_ID"]))
    client = str(UUID(env["CONTOSO_SDK_EXPECTED_CLIENT_ID"]))
    connection = env["CONTOSO_STUDIO_SDK_CONNECTION_URL"]
    normalized = environment.replace("-", "")
    expected = (
        f"https://{normalized[:-2]}.{normalized[-2:]}.environment.api.powerplatform.com"
        "/copilotstudio/agenticruntime/3p/dataverse-backed/authenticated/bots/"
        f"{schema}/conversations?api-version=2022-03-01-preview"
    )
    if connection != expected:
        raise ValueError("Unexpected SDK channel target")
    return tenant, principal, client, connection


def credential():
    from azure.identity import ManagedIdentityCredential

    return ManagedIdentityCredential(
        connection_timeout=3, read_timeout=8, retry_total=0,
    )


class TransportDiagnostics:
    MAX_REDIRECTS = 8
    REDIRECT_STATUSES = (301, 302, 303, 307, 308)

    def __init__(self):
        self.redirect_statuses = []
        self.final_method = None

    def trace(self):
        from aiohttp import TraceConfig

        trace = TraceConfig()

        async def start(session, context, params):
            self.redirect_statuses = []
            self.final_method = None

        async def redirect(session, context, params):
            status = params.response.status
            if (
                type(status) is int and status in self.REDIRECT_STATUSES
                and len(self.redirect_statuses) < self.MAX_REDIRECTS
            ):
                self.redirect_statuses.append(status)

        async def end(session, context, params):
            self.final_method = self.method(params.response.method)

        async def failed(session, context, params):
            info = getattr(params.exception, "request_info", None)
            if info is not None:
                self.final_method = self.method(getattr(info, "method", None))

        trace.on_request_start.append(start)
        trace.on_request_redirect.append(redirect)
        trace.on_request_end.append(end)
        trace.on_request_exception.append(failed)
        return trace

    @staticmethod
    def method(value):
        return value if type(value) is str and value in ("GET", "POST") else "other"

    def projection(self):
        result = {
            "studio_redirect_statuses": [
                status for status in self.redirect_statuses[:self.MAX_REDIRECTS]
                if type(status) is int and status in self.REDIRECT_STATUSES
            ],
        }
        if self.final_method is not None:
            result["studio_final_method"] = self.method(self.final_method)
        return result


def sdk_client(environment, schema, token, env=None):
    from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

    env = os.environ if env is None else env
    expected_environment, expected_schema, _ = configuration(env)
    _, _, _, connection = proof_bindings(env)
    if environment != expected_environment or schema != expected_schema:
        raise ValueError("Unexpected SDK channel target")
    diagnostics = TransportDiagnostics()
    client = CopilotClient(ConnectionSettings(
        environment_id=environment, agent_identifier=schema, enable_diagnostics=False,
        direct_connect_url=connection,
        client_session_settings={
            "raise_for_status": True, "trace_configs": [diagnostics.trace()],
        },
    ), token)
    client._contoso_transport_diagnostics = diagnostics
    return client


async def conversation(client, progress=None):
    conversation_id = None
    size = 0
    message_count = 0
    activities = 0
    digest = hashlib.sha256()
    texts = []
    if progress is not None:
        progress["studio_operation"] = "start-conversation"
    async for activity in client.start_conversation(True):
        activities += 1
        if activities > 100:
            raise ValueError("Studio activity limit exceeded")
        candidate = getattr(getattr(activity, "conversation", None), "id", None)
        if candidate is not None:
            if (
                not isinstance(candidate, str) or not candidate.strip()
                or len(candidate) > 1024
                or conversation_id is not None and conversation_id != candidate
            ):
                raise ValueError("Invalid Studio conversation")
            conversation_id = candidate
    if conversation_id is None:
        raise ValueError("Missing Studio conversation")
    if progress is not None:
        progress["studio_operation"] = "ask-question"
    async for activity in client.ask_question(QUESTION, conversation_id):
        activities += 1
        if activities > 100:
            raise ValueError("Studio activity limit exceeded")
        candidate = getattr(getattr(activity, "conversation", None), "id", None)
        if candidate is not None and candidate != conversation_id:
            raise ValueError("Studio conversation changed")
        text = getattr(activity, "text", None)
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("Invalid Studio text")
            size += len(text.encode("utf-8"))
            if size > 65536:
                raise ValueError("Studio response limit exceeded")
            if getattr(activity, "type", None) == "message" and text.strip():
                message_count += 1
                digest.update(text.encode("utf-8"))
                texts.append(text)
    text = "\n".join(texts)
    return {
        "conversation_started": True, "message_count": message_count,
        "response_bytes": size, "nonempty_message_received": message_count > 0,
        "response_sha256": digest.hexdigest(),
        "source_id_mentioned": "CONTOSO-DEMO-SUPPLIER-001" in text,
        "partial_delivery_mentioned": all(number in text for number in ("300", "200", "500")),
    }


async def prove(request, cancellation, env, *, context_factory=current_platform_context,
                credential_factory=credential, token_inspector=None,
                client_factory=None):
    environment, schema, callers = configuration(env)
    _, expected_principal, _, _ = proof_bindings(env)
    caller = require_platform_caller(context_factory())
    question, variant = parse_input(request)
    if caller not in callers or question != "CONTOSO identity probe" or variant is not None:
        raise AccessDenied("This isolated SDK proof accepts only the admitted fixed operator probe")
    if cancellation.is_set():
        raise asyncio.CancelledError
    result = {
        "status": "sdk-s2s-proof", "stage": "credential", "studio_attempted": False,
        "expected_sdk_identity": expected_principal, "platform_caller_id": caller,
    }
    stage = "credential"
    diagnostics = None
    try:
        async with asyncio.timeout(32):
            resource_credential = credential_factory()
            try:
                token = await asyncio.to_thread(
                    resource_credential.get_token, RESOURCE + "/.default",
                )
                stage = "token-metadata"
                inspector = token_inspector or (lambda value: issued_token_metadata(value, env))
                identity = await asyncio.to_thread(inspector, token.token)
                identity["resource_authorized"] = False
                result["identity"] = identity
                if not identity["sdk_identity_matched"]:
                    return {**result, "stage": "workload-identity-mismatch"}
                if not identity["application_only"] or not identity["required_role_present"]:
                    return {**result, "stage": "application-permission-missing"}
                if cancellation.is_set():
                    raise asyncio.CancelledError
                stage = "studio"
                result["studio_attempted"] = True
                factory = client_factory or (
                    lambda environment, schema, value: sdk_client(environment, schema, value, env)
                )
                client = factory(environment, schema, token.token)
                candidate = getattr(client, "_contoso_transport_diagnostics", None)
                if isinstance(candidate, TransportDiagnostics):
                    diagnostics = candidate
                result["conversation"] = await conversation(client, result)
                if diagnostics is not None:
                    result.update(diagnostics.projection())
                identity["resource_authorized"] = True
                return {**result, "stage": "completed"}
            finally:
                await asyncio.to_thread(resource_credential.close)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if diagnostics is not None:
            result.update(diagnostics.projection())
        name = type(error).__name__
        status = getattr(error, "status", None)
        if type(status) is int and 400 <= status <= 599:
            result["studio_http_status"] = status
        return {
            **result, "stage": stage + "-failed",
            "error_type": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,95}", name) else "Error",
        }


def main():
    async def resolve(request, cancellation):
        return await prove(request, cancellation, os.environ)

    build_json_probe_host(resolve).run(port=8088)


if __name__ == "__main__":
    main()
