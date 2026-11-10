import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import zipfile

from contoso.ops.create_image import source_archive
from contoso.providers import sdk_proof as proof
from contoso.router.core import AccessDenied


ENVIRONMENT = "3b28e633-ec3b-41cb-a445-19d6b420049f"
CALLER = "44444444-4444-4444-8444-444444444444"
TENANT = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
CLIENT_ID = "33333333-3333-4333-8333-333333333333"
SCHEMA = "synthetic_agent"
CONNECTION = (
    "https://3b28e633ec3b41cba44519d6b42004.9f.environment.api.powerplatform.com"
    "/copilotstudio/agenticruntime/3p/dataverse-backed/authenticated/bots/"
    f"{SCHEMA}/conversations?api-version=2022-03-01-preview"
)


def environment():
    return {
        "CONTOSO_ENTRA_TENANT_ID": TENANT,
        "CONTOSO_STUDIO_ENVIRONMENT_ID": ENVIRONMENT,
        "CONTOSO_STUDIO_HARNESS_ENDPOINT": (
            f"{proof.RESOURCE}/copilotstudio-a2a/tenants/{TENANT}"
            f"/environments/{ENVIRONMENT}/agents/{SCHEMA}"
        ),
        "CONTOSO_PLATFORM_CALLER_IDS": json.dumps([CALLER]),
        "CONTOSO_SDK_EXPECTED_PRINCIPAL_ID": PRINCIPAL,
        "CONTOSO_SDK_EXPECTED_CLIENT_ID": CLIENT_ID,
        "CONTOSO_STUDIO_SDK_CONNECTION_URL": CONNECTION,
    }


class Credential:
    scopes = []
    closed = False

    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token="opaque-only-in-memory")

    def close(self):
        self.closed = True


class Client:
    async def start_conversation(self, emit):
        yield SimpleNamespace(conversation=SimpleNamespace(id="conversation"))

    async def ask_question(self, question, conversation_id):
        assert question == proof.QUESTION
        assert conversation_id == "conversation"
        yield SimpleNamespace(
            type="message", conversation=SimpleNamespace(id=conversation_id),
            text="300 of 500, remaining 200; CONTOSO-DEMO-SUPPLIER-001",
        )


class ProofTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_uses_exact_app_channel_connection(self):
        client = proof.sdk_client(ENVIRONMENT, SCHEMA, "test-token", environment())
        self.assertEqual(client.settings.direct_connect_url, CONNECTION)
        self.assertIn("/agenticruntime/3p/", client.settings.direct_connect_url)
        self.assertFalse(client.settings.enable_diagnostics)
        self.assertIs(client.settings.client_session_settings["raise_for_status"], True)

    async def test_sdk_rejects_another_channel_target(self):
        with self.assertRaises(ValueError):
            proof.sdk_client(ENVIRONMENT, "other_schema", "test-token", environment())

    def test_missing_or_mismatched_trusted_bindings_fail_closed(self):
        for key in ("CONTOSO_ENTRA_TENANT_ID", "CONTOSO_STUDIO_ENVIRONMENT_ID",
                    "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_SDK_EXPECTED_PRINCIPAL_ID",
                    "CONTOSO_SDK_EXPECTED_CLIENT_ID", "CONTOSO_STUDIO_SDK_CONNECTION_URL"):
            env = environment()
            del env[key]
            with self.subTest(missing=key), self.assertRaises((KeyError, ValueError)):
                proof.proof_bindings(env)
        for suffix in ("&untrusted=true", "#fragment", "/other"):
            env = environment()
            env["CONTOSO_STUDIO_SDK_CONNECTION_URL"] += suffix
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                proof.proof_bindings(env)

    async def loopback(self, statuses, final_method):
        from aiohttp import web

        observed = []

        async def handler(request):
            observed.append(request.method)
            index = int(request.match_info["index"])
            if index < len(statuses):
                return web.Response(
                    status=statuses[index],
                    headers={"Location": f"/{index + 1}?sensitive-query"},
                )
            return web.Response(
                status=405, text="sensitive-response",
                headers={"X-Sensitive": "sensitive-header"},
            )

        app = web.Application()
        app.router.add_route("*", "/{index}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            client = proof.sdk_client(ENVIRONMENT, SCHEMA, "sensitive-token", environment())
            client._resolve_connection_url = AsyncMock(
                return_value=f"http://127.0.0.1:{port}/0",
            )
            result = await self.invoke(client_factory=lambda *args: client)
        finally:
            await runner.cleanup()
        self.assertEqual(observed[0], "POST")
        self.assertEqual(observed[-1], final_method)
        self.assertEqual(result["studio_final_method"], final_method)
        self.assertEqual(result["studio_redirect_statuses"], statuses[:8])
        self.assertEqual(result["studio_http_status"], 405)
        self.assertEqual(result["error_type"], "ClientResponseError")
        self.assertEqual(result["studio_operation"], "start-conversation")
        self.assertFalse(result["identity"]["resource_authorized"])
        serialized = json.dumps(result)
        for forbidden in ("sensitive", "127.0.0.1", "Location", "request_info", "history"):
            self.assertNotIn(forbidden, serialized)
        return result

    async def test_actual_sdk_redirect_301_changes_post_to_get(self):
        await self.loopback([301], "GET")

    async def test_actual_sdk_redirect_302_changes_post_to_get(self):
        await self.loopback([302], "GET")

    async def test_actual_sdk_redirect_303_changes_post_to_get(self):
        await self.loopback([303], "GET")

    async def test_actual_sdk_redirect_307_preserves_post(self):
        await self.loopback([307], "POST")

    async def test_actual_sdk_redirect_308_preserves_post(self):
        await self.loopback([308], "POST")

    async def test_actual_sdk_redirect_chain_is_truncated_without_changing_transport(self):
        await self.loopback([307] * 9, "POST")

    async def test_transport_trace_resets_between_operations_and_sanitizes(self):
        diagnostics = proof.TransportDiagnostics()
        trace = diagnostics.trace()
        diagnostics.redirect_statuses = [301]
        diagnostics.final_method = "GET"
        await trace.on_request_start[0](None, None, SimpleNamespace())
        self.assertEqual(diagnostics.projection(), {"studio_redirect_statuses": []})
        for status in ("302", True, 999, 302):
            await trace.on_request_redirect[0](
                None, None, SimpleNamespace(response=SimpleNamespace(status=status)),
            )
        await trace.on_request_end[0](
            None, None, SimpleNamespace(response=SimpleNamespace(method="sensitive-method")),
        )
        self.assertEqual(diagnostics.projection(), {
            "studio_redirect_statuses": [302], "studio_final_method": "other",
        })
        self.assertNotIn("sensitive", json.dumps(diagnostics.projection()))

    async def invoke(self, *, identity=None, caller=CALLER, request=None,
                     client_factory=None, credential_factory=None):
        self.credential = Credential()
        self.credential.scopes = []
        context = SimpleNamespace(
            user_id=caller, platform_headers=lambda: {"x-agent-foundry-call-id": "fixed"},
        )
        return await proof.prove(
            request or {"input": "CONTOSO identity probe", "store": False},
            asyncio.Event(), environment(), context_factory=lambda: context,
            credential_factory=credential_factory or (lambda: self.credential),
            token_inspector=lambda token: identity or {
                "sdk_identity_matched": True, "application_only": True,
                "required_role_present": True,
            },
            client_factory=client_factory or (lambda *args: Client()),
        )

    async def test_real_transport_boundary_uses_resource_default_and_closes(self):
        result = await self.invoke()
        self.assertEqual(self.credential.scopes, [proof.RESOURCE + "/.default"])
        self.assertTrue(self.credential.closed)
        self.assertEqual(result["stage"], "completed")
        self.assertTrue(result["conversation"]["source_id_mentioned"])
        self.assertTrue(result["identity"]["resource_authorized"])
        self.assertNotIn("opaque-only-in-memory", json.dumps(result))
        self.assertNotIn("300 of 500", json.dumps(result))

    async def test_wrong_workload_never_calls_studio(self):
        result = await self.invoke(
            identity={"sdk_identity_matched": False},
            client_factory=lambda *args: self.fail("Studio must not be called"),
        )
        self.assertEqual(result["stage"], "workload-identity-mismatch")
        self.assertFalse(result["studio_attempted"])

    async def test_missing_role_never_calls_studio(self):
        result = await self.invoke(identity={
            "sdk_identity_matched": True, "application_only": True,
            "required_role_present": False,
        })
        self.assertEqual(result["stage"], "application-permission-missing")

    async def test_delegated_token_never_calls_studio(self):
        result = await self.invoke(identity={
            "sdk_identity_matched": True, "application_only": False,
            "required_role_present": True,
        })
        self.assertFalse(result["studio_attempted"])

    async def test_unauthorized_caller_cannot_acquire_credentials(self):
        with self.assertRaises(AccessDenied):
            await self.invoke(caller="not-admitted")

    async def test_arbitrary_question_rejected(self):
        with self.assertRaises(AccessDenied):
            await self.invoke(request={"input": "invoke another agent"})

    async def test_credential_failure_is_sanitized(self):
        def unavailable():
            raise RuntimeError("secret details must not leave the worker")
        result = await self.invoke(credential_factory=unavailable)
        self.assertEqual(result["stage"], "credential-failed")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(result))

    async def test_studio_failure_does_not_confirm_resource_authorization(self):
        def unavailable(*args):
            raise RuntimeError("secret details")
        result = await self.invoke(client_factory=unavailable)
        self.assertEqual(result["stage"], "studio-failed")
        self.assertTrue(result["studio_attempted"])
        self.assertFalse(result["identity"]["resource_authorized"])
        self.assertNotIn("secret", json.dumps(result))

    async def test_studio_failure_returns_only_http_status_and_fixed_operation(self):
        class Rejected(Client):
            async def start_conversation(self, emit):
                error = RuntimeError("sensitive upstream message")
                error.status = 403
                raise error
                yield
        result = await self.invoke(client_factory=lambda *args: Rejected())
        self.assertEqual(result["studio_operation"], "start-conversation")
        self.assertEqual(result["studio_http_status"], 403)
        self.assertFalse(result["identity"]["resource_authorized"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_fixed_target_rejects_ssrf(self):
        env = environment()
        env["CONTOSO_STUDIO_HARNESS_ENDPOINT"] = "https://evil.example/agent"
        with self.assertRaises(ValueError):
            proof.configuration(env)

    def test_proof_image_does_not_change_default_runtime_command(self):
        root = Path(__file__).resolve().parents[3]
        with zipfile.ZipFile(io.BytesIO(source_archive(root))) as normal:
            self.assertIn(b'"contoso.entrypoint"', normal.read("contoso/Dockerfile"))
        with zipfile.ZipFile(io.BytesIO(source_archive(root, sdk_proof=True))) as isolated:
            self.assertIn(b'"contoso.providers.sdk_proof"', isolated.read("contoso/Dockerfile"))
            self.assertIn("contoso/providers/sdk_proof.py", isolated.namelist())


class IssuedTokenMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(self, **overrides):
        import time
        return proof.jwt.encode({
            "iss": f"https://sts.windows.net/{TENANT}/",
            "aud": proof.RESOURCE, "tid": TENANT,
            "oid": PRINCIPAL, "appid": CLIENT_ID,
            "roles": [proof.ROLE], "iat": int(time.time()), "exp": int(time.time()) + 60,
            **overrides,
        }, self.key, algorithm="RS256")

    def verify(self, token):
        return proof.issued_token_metadata(token, environment())

    def test_diagnostic_claims_do_not_prove_signature_or_resource_authorization(self):
        result = self.verify(self.token())
        self.assertTrue(result["sdk_identity_matched"])
        self.assertFalse(result["signature_validated"])
        self.assertFalse(result["resource_authorized"])
        self.assertEqual(result["metadata_source"], "issued-token-diagnostic")
        self.assertTrue(result["required_role_present"])

    def test_project_identity_is_not_sdk_identity(self):
        result = self.verify(self.token(
            oid="44444444-4444-4444-8444-444444444444",
            appid="44444444-4444-4444-8444-444444444444",
        ))
        self.assertFalse(result["sdk_identity_matched"])

    def test_both_expected_identity_bindings_are_required(self):
        for overrides in ({"oid": CLIENT_ID}, {"appid": PRINCIPAL}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.verify(self.token(**overrides))["sdk_identity_matched"])

    def test_different_tenant_is_rejected(self):
        with self.assertRaises(ValueError):
            self.verify(self.token(tid="55555555-5555-4555-8555-555555555555"))

    def test_wrong_audience_and_expired_tokens_rejected(self):
        for token in (self.token(aud="https://graph.microsoft.com"),
                      self.token(exp=1)):
            with self.assertRaises(proof.jwt.InvalidTokenError):
                self.verify(token)

    def test_user_scope_is_not_application_only(self):
        self.assertFalse(self.verify(self.token(scp="delegated"))["application_only"])
