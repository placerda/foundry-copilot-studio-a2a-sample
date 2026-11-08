from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import errno
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from contoso.publication import CommandResult, PublicationFailure
from contoso.publication.auth import AUTH_PROBE_SCOPE, AuthEnvironment, token_response
from contoso.publication.diagnostics import (
    MAX_STREAM_BYTES, SAFE_ERROR_CODES, CommandEnvironment, _Stream, bounded_command, classify_azd_failure,
)
from contoso.publication.tests.test_executor import (
    auth, config, executor, linux_package, package_snapshot, assert_snapshot, sha,
)


@pytest.mark.parametrize("message,expected", [
    ("AuthenticationFailed bearer SECRET", "azd_authentication_failed"),
    ("not logged in: run azd auth login SECRET", "azd_not_logged_in"),
    ("missing credential SECRET", "azd_credential_unavailable"),
    ("fork/exec /secret/azd: permission denied", "azd_executable_permission_denied"),
    ("exec format error SECRET", "azd_executable_format_invalid"),
    ("executable file not found SECRET", "azd_executable_unavailable"),
    ("AzureDeveloperCLICredential: Azure Developer CLI not found on path SECRET",
     "azd_executable_unavailable"),
    ("/bin/sh: 1: azd: not found SECRET", "azd_executable_unavailable"),
    ("rpc error: code = Unimplemented desc = SECRET", "azd_extension_rpc_unavailable"),
    ("no space left on device SECRET", "azd_disk_full"),
    ("Dockerfile not found SECRET", "azd_package_source_missing"),
    ("cannot connect to the Docker daemon SECRET", "azd_container_tool_unavailable"),
    ("tenant_lookup_failed SECRET", "azd_tenant_lookup_failed"),
    ("developer_missing_ai_user_role SECRET", "azd_developer_missing_ai_user_role"),
    ("fetching current user information: SECRET", "azd_auth_principal_unavailable"),
    ("AuthorizationFailed: operation SECRET", "azd_authorization_failed"),
    ("AuthorizationFailed: Microsoft.Resources/subscriptions/read SECRET",
     "azd_subscription_read_forbidden"),
    ("AuthorizationFailed: Microsoft.Resources/subscriptions/resourceGroups/read SECRET",
     "azd_resource_group_read_forbidden"),
    ("AuthorizationFailed: microsoft.resources/subscriptions/resourcegroups/read SECRET",
     "azd_resource_group_read_forbidden"),
    ("AuthorizationFailed: Microsoft.Authorization/permissions/read SECRET",
     "azd_permission_read_forbidden"),
    ("GET https://management.azure.com/SECRET RESPONSE 403: Forbidden",
     "azd_management_endpoint_forbidden"),
    ("GET https://SECRET.services.ai.azure.com/SECRET RESPONSE 403: Forbidden",
     "azd_foundry_endpoint_forbidden"),
    ("AuthorizationFailed: Microsoft.CognitiveServices/accounts/read SECRET",
     "azd_foundry_account_read_forbidden"),
    ("AuthorizationFailed: Microsoft.CognitiveServices/accounts/projects/read SECRET",
     "azd_project_read_forbidden"),
    ("AuthorizationFailed: Microsoft.ContainerRegistry/registries/read SECRET",
     "azd_registry_read_forbidden"),
    ("AuthorizationFailed: Microsoft.Authorization/roleAssignments/read SECRET",
     "azd_role_assignment_read_forbidden"),
    ("developer_missing_role_assign_write_role: Forbidden SECRET",
     "azd_developer_missing_role_assign_write_role"),
    ("extension azure.ai.agents not installed SECRET", "azd_extension_missing"),
    ("unknown flag: --secret", "azd_invalid_flags"),
    ("invalid YAML: accessToken SECRET", "azd_schema_invalid"),
    ("required environment variable missing SECRET", "azd_environment_binding_invalid"),
    ("AuthorizationFailed: Microsoft.Authorization/roleAssignments/write SECRET", "azd_role_assignment_write_forbidden"),
    ("PublicNetworkAccessDisabled SECRET", "azd_private_network_failure"),
    ("missing unique_name claim SECRET", "azd_auth_principal_unavailable"),
    ("external auth token did not contain a usable account identifier: no current user",
     "azd_auth_principal_unavailable"),
])
def test_static_failure_codes_never_reflect_messages(message, expected):
    code = classify_azd_failure(b"", message.encode())
    assert code == expected and code in SAFE_ERROR_CODES
    assert "SECRET" not in code and "/" not in code


@pytest.mark.parametrize("event", [
    {"error": {"code": "GetTokenError", "message": "SECRET"}},
    {"type": "error", "code": "GetTokenError", "message": "SECRET"},
    {"status": "error", "code": "GetTokenError", "message": "SECRET"},
    {"error": {"code": "UnknownSecret", "innererror": {"code": "GetTokenError", "message": "SECRET"}}},
])
def test_allowlisted_structured_error_codes(event):
    assert classify_azd_failure(json.dumps(event).encode(), b"") == "azd_credential_unavailable"


def test_structured_authorization_detail_precedes_generic_forbidden():
    event = {"error": {"code": "agent_identity_rbac_failed", "message": "Forbidden SECRET"}}
    assert classify_azd_failure(json.dumps(event).encode(), b"") == "azd_agent_identity_rbac_failed"


def test_resource_error_detail_precedes_endpoint_category():
    event = {"error": {
        "code": "ForbiddenByRbac",
        "message": "GET https://SECRET.services.ai.azure.com/SECRET Forbidden",
    }}
    assert classify_azd_failure(json.dumps(event).encode(), b"") == "azd_resource_rbac_forbidden"


@pytest.mark.parametrize("event", [
    {"error": {"code": "SECRET", "message": "SECRET"}},
    {"error": {"code": ["SECRET"]}},
    {"status": "success", "code": "GetTokenError"},
    {"error": {"code": {"SECRET": "SECRET"}}},
])
def test_unknown_or_malformed_structured_codes_are_not_reflected(event):
    assert classify_azd_failure(json.dumps(event).encode(), b"") is None


def test_stream_ring_is_bounded_and_retains_last_failure():
    stream = _Stream()
    stream.read(io.BytesIO(b"S" * (MAX_STREAM_BYTES * 4) + b"\nunknown flag: SECRET"))
    assert stream.truncated and len(stream.bytes) == MAX_STREAM_BYTES
    assert classify_azd_failure(stream.bytes, b"") == "azd_invalid_flags"


def fake_process(monkeypatch, *, stdout=b"", stderr=b"", code=0):
    def popen(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        return type("Process", (), {
            "stdout": io.BytesIO(stdout), "stderr": io.BytesIO(stderr),
            "wait": lambda self, **kwargs: code, "poll": lambda self: code,
        })()
    monkeypatch.setattr(subprocess, "Popen", popen)


def test_zero_exit_user_status_is_not_an_mi_authentication_failure(config, monkeypatch):
    fake_process(monkeypatch, stdout=b'{"status":"unauthenticated"}',
                 stderr=b"missing unique_name claim SECRET")
    result = bounded_command(
        ["fixed", "auth", "status", "--output", "json", "--no-prompt"],
        cwd=config.package_root, env={}, timeout=30,
    )
    assert result.returncode == 0 and result.error_code is None
    assert not result.auth_authenticated and "SECRET" not in repr(result)
    assert not result.token_credential_valid


def test_zero_exit_unknown_structured_failure_is_still_failure(config, monkeypatch):
    fake_process(monkeypatch, stdout=b'{"error":{"code":"SECRET","message":"SECRET"}}')
    result = bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=30)
    assert result == CommandResult(0, "azd_unclassified_error")


def test_rejected_private_broker_request_is_not_mislabeled_as_cloud_rbac(config, monkeypatch):
    fake_process(monkeypatch, stderr=b"Forbidden SECRET", code=1)
    observation = AuthEnvironment()
    observation.credential_rejected = True
    result = bounded_command(
        ["fixed"], cwd=config.package_root,
        env=CommandEnvironment({}, observation), timeout=30,
    )
    assert result == CommandResult(1, "azd_external_credential_request_rejected")
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("exit_code,expected", [
    (-9, "azd_process_killed"), (137, "azd_process_killed"),
    (-6, "azd_process_aborted"), (134, "azd_process_aborted"),
])
def test_process_termination_is_classified_without_guessing_oom(config, monkeypatch, exit_code, expected):
    fake_process(monkeypatch, code=exit_code, stderr=b"SECRET")
    result = bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=30)
    assert result == CommandResult(exit_code, expected)
    assert "SECRET" not in repr(result)


def test_zero_exit_ndjson_failure_cannot_become_success(config, monkeypatch):
    fake_process(
        monkeypatch,
        stdout=b'{"type":"message","message":"SECRET"}\n{"type":"error","error":{"code":"GetTokenError"}}\n',
    )
    result = bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=30)
    assert result == CommandResult(0, "azd_credential_unavailable")


def test_auth_status_projects_only_boolean_without_username_or_token(config, monkeypatch):
    fake_process(monkeypatch, stdout=b'{"status":"authenticated","email":"SECRET","accessToken":"SECRET"}')
    result = bounded_command(["fixed", "auth", "status"], cwd=config.package_root, env={}, timeout=30)
    assert result.auth_authenticated and "SECRET" not in json.dumps(asdict(result))


@pytest.mark.parametrize("version,valid", [
    ("1.37.2", True), ("1.38.0", True), ("1.36.9", False), ("SECRET", False), ("9" * 5000, False),
])
def test_version_contract_is_reduced_to_supported_boolean(config, monkeypatch, version, valid):
    fake_process(monkeypatch, stdout=json.dumps({"azd": {"version": version, "commit": "SECRET"}}).encode())
    result = bounded_command(["fixed", "version", "--output", "json"], cwd=config.package_root, env={}, timeout=30)
    assert result.version_valid is valid and "SECRET" not in repr(result)


def test_environment_contract_requires_exact_approved_values(config, monkeypatch):
    values = config.values()
    for changed in (None, "missing", "incorrect", "extra"):
        reported = values.copy()
        if changed == "missing":
            del reported["AZURE_AI_PROJECT_ENDPOINT"]
        elif changed == "incorrect":
            reported["AZURE_ENV_NAME"] = "other"
        elif changed == "extra":
            reported["ACCESS_TOKEN"] = "SECRET"
        fake_process(monkeypatch, stdout=json.dumps(reported).encode())
        result = bounded_command(
            ["fixed", "env", "get-values", "--environment", config.environment, "--output", "json"],
            cwd=config.package_root, env={**values, "AZURE_DEV_USER_AGENT": "SECRET"}, timeout=30,
        )
        assert result.environment_bound is (changed is None)
        assert "SECRET" not in repr(result)


def test_truncated_or_ambiguous_success_is_not_accepted(config, monkeypatch):
    for output in (
        b"S" * MAX_STREAM_BYTES + b'{"status":"authenticated"}',
        b'{"status":"unauthenticated","status":"authenticated"}',
    ):
        fake_process(monkeypatch, stdout=output)
        result = bounded_command(["fixed", "auth", "status"], cwd=config.package_root, env={}, timeout=30)
        assert not result.auth_authenticated


@pytest.mark.parametrize("number,expected", [
    (errno.EACCES, "azd_executable_permission_denied"),
    (errno.ENOEXEC, "azd_executable_format_invalid"),
    (errno.ENOENT, "azd_executable_unavailable"),
])
def test_spawn_failure_codes_are_safe(config, monkeypatch, number, expected):
    def fail(*args, **kwargs):
        raise OSError(number, "SECRET")
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(PublicationFailure, match=expected) as error:
        bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=30)
    assert "SECRET" not in str(error.value)


def test_real_bounded_runner_drains_both_streams_without_logging(config):
    result = bounded_command(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('SECRET'*50000); sys.stderr.write('unknown flag: SECRET'); sys.exit(9)"],
        cwd=config.package_root, env={}, timeout=30,
    )
    assert result == CommandResult(9, "azd_invalid_flags")
    assert "SECRET" not in repr(result)


def test_real_bounded_timeout_is_safe_and_terminates_owned_process(config):
    with pytest.raises(PublicationFailure, match="deployment_timeout"):
        bounded_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=config.package_root, env={}, timeout=0.2,
        )


def diagnostic_runner(config, calls, *, auth_result=None, observe=True):
    def command(argv, *, cwd, env, timeout):
        calls.append(argv[1:])
        assert argv[0] == str(cwd / "bin" / "azd")
        assert timeout <= 30 and env["AZURE_DEV_USER_AGENT"] == "one-shot-test"
        assert env["AZD_AGENT_SKIP_ROLE_ASSIGNMENTS"] == "true"
        assert "AZD_AGENT_SKIP_ROLE_ASSIGNMENTS" not in config.values()
        assert env["HOME"] == str(cwd) and env["AZD_CONFIG_DIR"] == str(cwd / "azd-config")
        assert env["AZD_AUTH_ENDPOINT"].startswith("unix:")
        assert not (cwd / "azd-config" / "auth.json").exists()
        assert not any("login" in word or word == "--debug" for word in argv)
        if argv[1] == "version":
            return CommandResult(0, version_valid=True)
        if argv[1:3] == ["env", "get-values"]:
            from dotenv import dotenv_values
            assert dotenv_values(cwd / ".azure" / config.environment / ".env", interpolate=False) == config.values()
            return CommandResult(0, environment_bound=True)
        assert argv[1:] == [
            "auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", config.tenant_id,
            "--output", "json", "--no-prompt",
        ]
        assert env.credential_observation is not None
        assert "credential_observation" not in env
        return auth_result or CommandResult(0, token_credential_valid=True)

    @contextmanager
    def observed(workspace, credential, tenant, scopes):
        assert credential == "fixed-mi" and tenant == config.tenant_id
        with auth(workspace, credential, tenant, scopes) as env:
            observation = AuthEnvironment(env)
            # Injection represents observation of a genuine broker request in production.
            observation.credential_requested = observe
            observation.credential_observed = observe
            yield observation

    return command, observed


def test_fixed_diagnostic_self_test_uses_same_preparation_and_broker(config):
    configured = linux_package(config)
    snapshot = package_snapshot(configured)
    calls, writes = [], []
    command, context = diagnostic_runner(configured, calls)
    worker = executor(configured, command=command, job_store=lambda data: writes.append(data))
    worker.auth_context = context
    worker.preflight = lambda: pytest.fail("Self-test must not call project, agent or Blob services")
    result = worker.diagnostic_self_test(process_user_agent="one-shot-test")
    assert result["status"] == "passed" and result["error_code"] is None
    assert all(type(value) is bool and value for value in result["checks"].values())
    assert calls == [
        ["version", "--output", "json", "--no-prompt"],
        ["env", "get-values", "--environment", configured.environment, "--output", "json", "--no-prompt"],
        ["auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", configured.tenant_id,
         "--output", "json", "--no-prompt"],
    ]
    assert result["limitation_code"] == "azd_external_principal_metadata_unsupported"
    assert writes == [] and not list(configured.workspace_root.iterdir())
    assert_snapshot(snapshot)


@pytest.mark.parametrize("observe,auth_result,expected", [
    (True, CommandResult(0), "azd_external_credential_roundtrip_unconfirmed"),
    (False, CommandResult(0, token_credential_valid=True), "azd_external_credential_not_observed"),
    (True, CommandResult(0, auth_authenticated=True), "azd_external_credential_roundtrip_unconfirmed"),
    (True, CommandResult(1, "azd_auth_principal_unavailable"), "azd_auth_principal_unavailable"),
    (False, CommandResult(1, "azd_credential_unavailable"), "azd_credential_unavailable"),
])
def test_self_test_requires_client_roundtrip_not_just_broker_or_user_status(config, observe, auth_result, expected):
    configured = linux_package(config)
    calls = []
    command, context = diagnostic_runner(configured, calls, observe=observe, auth_result=auth_result)
    worker = executor(configured, command=command)
    worker.auth_context = context
    result = worker.diagnostic_self_test(process_user_agent="one-shot-test")
    assert result["status"] == "failed" and result["error_code"] == expected
    assert result["checks"]["managed_identity_credential"] is observe
    assert not result["checks"]["azd_credential"]
    assert "SECRET" not in json.dumps(result)


def test_self_test_registration_and_manifest_bindings_fail_closed(config):
    configured = linux_package(config)
    path = configured.package_root / "azure.yaml"
    original = path.read_text()
    path.write_text(original + '\nmetadata:\n  reference: "${UNAPPROVED_BINDING}"\n')
    configured = replace(configured, azure_yaml_sha256=sha(path.read_bytes()))
    calls = []
    result = executor(configured, command=lambda *a, **k: calls.append(a)).diagnostic_self_test(
        process_user_agent="one-shot-test",
    )
    assert result["error_code"] == "azd_environment_binding_invalid" and calls == []


def test_self_test_missing_extension_registration_is_not_implicit_install(config):
    calls = []
    result = executor(config, command=lambda *a, **k: calls.append(a)).diagnostic_self_test(
        process_user_agent="one-shot-test",
    )
    assert result["status"] == "failed" and not result["checks"]["extension_registration"]
    assert calls == []


def test_shared_run_rejects_unapproved_command_before_execution(config):
    configured = linux_package(config)
    calls = []
    worker = executor(configured, command=lambda *a, **k: calls.append(a))
    workspace = configured.workspace_root / "fixed-diagnostic"
    executable, pins = worker._prepare_workspace(workspace, private_preflight=False)
    try:
        for arguments in (
            ["auth", "login"], ["deploy", "unknown"], ["extension", "install", "unknown"],
            ["env", "get-values", "--environment", "other"],
            ["auth", "status", "--output", "json", "--no-prompt"],
            ["auth", "token", "--scope", "https://evil.example/.default",
             "--tenant-id", configured.tenant_id, "--output", "json", "--no-prompt"],
            ["auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", "other",
             "--output", "json", "--no-prompt"],
        ):
            with pytest.raises(PublicationFailure, match="azd_command_not_approved"):
                worker._run(workspace, executable, pins, {}, arguments, failure="deployment_nonzero_exit")
        assert calls == []
    finally:
        import shutil
        shutil.rmtree(workspace)


def test_broker_empty_or_expired_tokens_never_observe_success():
    for token, expiration in (("", 2000000000), ("SECRET", 1)):
        credential = type("Credential", (), {
            "get_token": lambda self, *scopes: type("Token", (), {"token": token, "expires_on": expiration})(),
        })()
        with pytest.raises(ValueError):
            token_response(credential, {"scopes": ["https://ai.azure.com/.default"]},
                           "tenant", frozenset({"https://ai.azure.com/.default"}))


def test_durable_publish_preserves_fixed_diagnostic_failure(config):
    writes = []
    worker = executor(
        config, command=lambda *a, **k: CommandResult(1, "azd_role_assignment_write_forbidden"),
        job_store=lambda data: writes.append(data.copy()),
    )
    job = worker.publish_service("contoso-knowledge-agent", process_user_agent="one-shot-test")
    assert job.status == "failed" and job.error_code == "azd_role_assignment_write_forbidden"
    assert writes[-1] == job.as_dict() and job.services == {}


def test_command_result_rejects_unknown_diagnostic_or_raw_facts():
    with pytest.raises(ValueError):
        CommandResult(1, "SECRET")
    with pytest.raises(ValueError):
        CommandResult(0, auth_authenticated="SECRET")
    with pytest.raises(ValueError):
        CommandResult(0, token_credential_valid="SECRET")


def recorded_credential(*, token="OPAQUE_MI_SECRET", expiration=2000000000, scope=AUTH_PROBE_SCOPE):
    observation = AuthEnvironment()
    request = {"scopes": [scope]}
    result = {
        "status": "success", "token": token,
        "expiresOn": datetime.fromtimestamp(expiration, timezone.utc).isoformat(),
    }
    observation.record_token(request, result)
    return observation


def test_real_remote_contract_roundtrip_needs_no_upn_or_fabricated_claims(config, monkeypatch):
    observation = recorded_credential()
    fake_process(monkeypatch, stdout=b'{"token":"OPAQUE_MI_SECRET","expiresOn":"2033-05-18T03:33:20Z"}')
    result = bounded_command(
        ["fixed", "auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", config.tenant_id,
         "--output", "json", "--no-prompt"],
        cwd=config.package_root, env=CommandEnvironment({}, observation), timeout=30,
    )
    assert result.token_credential_valid and not result.auth_authenticated
    assert result.error_code is None
    assert "SECRET" not in json.dumps(asdict(result))
    assert "token" not in observation and all(isinstance(digest, bytes) for digest in observation._issued)
    assert all("OPAQUE_MI_SECRET" not in repr(value) for value in observation._issued.values())
    observation.clear_issued()
    assert not observation.confirms_token("OPAQUE_MI_SECRET", 2000000000, AUTH_PROBE_SCOPE)


@pytest.mark.parametrize("token,expiration,complete", [
    ("DIFFERENT_SECRET", "2033-05-18T03:33:20Z", True),
    ("", "2033-05-18T03:33:20Z", True),
    ("OPAQUE_MI_SECRET", "2033-05-18T03:33:21Z", True),
    ("OPAQUE_MI_SECRET", "2000-01-01T00:00:00Z", True),
    ("OPAQUE_MI_SECRET", "2033-05-18T03:33:20", True),
    ("OPAQUE_MI_SECRET", "invalid-date-SECRET", True),
    ("OPAQUE_MI_SECRET", "2033-05-18T03:33:20Z", False),
])
def test_roundtrip_rejects_unissued_expired_malformed_or_truncated_token_output(
    config, monkeypatch, token, expiration, complete,
):
    observation = recorded_credential()
    output = json.dumps({"token": token, "expiresOn": expiration}).encode()
    if not complete:
        output = b"S" * MAX_STREAM_BYTES + output
    fake_process(monkeypatch, stdout=output)
    result = bounded_command(
        ["fixed", "auth", "token"], cwd=config.package_root,
        env=CommandEnvironment({}, observation), timeout=30,
    )
    assert not result.token_credential_valid and "SECRET" not in repr(result)


def test_roundtrip_rejects_wrong_scope_or_missing_actual_observation(config, monkeypatch):
    for observation in (None, recorded_credential(scope="https://ai.azure.com/.default")):
        fake_process(monkeypatch, stdout=b'{"token":"OPAQUE_MI_SECRET","expiresOn":"2033-05-18T03:33:20Z"}')
        result = bounded_command(
            ["fixed", "auth", "token"], cwd=config.package_root,
            env=CommandEnvironment({}, observation), timeout=30,
        )
        assert not result.token_credential_valid


def test_opaque_token_bytes_are_not_used_as_error_patterns(config, monkeypatch):
    token = "OPAQUE-AuthenticationFailed-GetTokenError-SECRET"
    observation = recorded_credential(token=token)
    fake_process(monkeypatch, stdout=json.dumps({
        "token": token, "expiresOn": "2033-05-18T03:33:20Z",
    }).encode(), code=1)
    result = bounded_command(
        ["fixed", "auth", "token"], cwd=config.package_root,
        env=CommandEnvironment({}, observation), timeout=30,
    )
    assert result.error_code is None and result.returncode == 1
    assert "SECRET" not in repr(result)


def test_broker_handler_error_envelopes_without_network_or_cloud(config, monkeypatch, request):
    import os
    import shutil
    import types
    from uuid import uuid4
    from contoso.publication import auth

    handlers = []

    class Server:
        def __init__(self, path, handler):
            Path(path).touch()
            handlers.append(handler)

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    class Credential:
        calls = 0
        fail = False

        def get_token(self, *scopes):
            self.calls += 1
            if self.fail:
                raise RuntimeError("SDK-SECRET")
            return types.SimpleNamespace(token="OPAQUE_MI_SECRET", expires_on=2000000000)

    monkeypatch.setattr(auth, "os", types.SimpleNamespace(name="posix", chmod=os.chmod))
    monkeypatch.setattr(auth.socketserver, "UnixStreamServer", Server, raising=False)
    workspace = Path("contoso") / "publication" / "tests" / "work" / ("h-" + uuid4().hex)
    workspace.mkdir(parents=True)
    request.addfinalizer(lambda: shutil.rmtree(workspace))
    credential = Credential()
    with auth.managed_auth(
        workspace, credential, config.tenant_id, frozenset({AUTH_PROBE_SCOPE}),
    ) as env:
        for fail, key, request, expected in (
            (False, env["AZD_AUTH_KEY"], {"scopes": [AUTH_PROBE_SCOPE]}, 200),
            (True, env["AZD_AUTH_KEY"], {"scopes": [AUTH_PROBE_SCOPE]}, 200),
            (False, "wrong", {"scopes": [AUTH_PROBE_SCOPE]}, 403),
            (False, env["AZD_AUTH_KEY"], {"scopes": [["unhashable"]]}, 403),
            (False, env["AZD_AUTH_KEY"], {"scopes": [AUTH_PROBE_SCOPE], "tenantId": "other"}, 403),
        ):
            credential.fail = fail
            instance = handlers[0].__new__(handlers[0])
            body = json.dumps(request).encode()
            instance.path = "/token?api-version=2023-07-12-preview"
            instance.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer " + key}
            instance.connection = types.SimpleNamespace(settimeout=lambda _: None)
            instance.rfile, instance.wfile = io.BytesIO(body), io.BytesIO()
            codes = []
            instance.send_response = codes.append
            instance.send_header = lambda *args: None
            instance.end_headers = lambda: None
            instance.do_POST()
            response = json.loads(instance.wfile.getvalue())
            assert codes == [expected] and "SDK-SECRET" not in json.dumps(response)
            if fail:
                assert response == {
                    "status": "error", "code": "GetTokenError",
                    "message": "Managed identity token acquisition failed.",
                }
            elif expected == 200:
                assert env.confirms_token(response["token"], 2000000000, AUTH_PROBE_SCOPE)
        assert credential.calls == 2
    assert env._issued == {}


@pytest.mark.skipif(sys.platform == "win32", reason="Production Unix-socket transport is Linux/macOS only")
def test_broker_wire_error_contract_and_correlation(config):
    import http.client
    import socket
    from contoso.publication.auth import managed_auth

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(self.host)

    class Credential:
        fail = False

        def get_token(self, *scopes):
            if self.fail:
                raise RuntimeError("SDK-SECRET")
            return type("Token", (), {"token": "OPAQUE_MI_SECRET", "expires_on": 2000000000})()

    # Keep below the production Unix path length limit.
    workspace = Path("contoso") / "publication" / "tests" / "work" / "broker-wire"
    workspace.mkdir(parents=True)
    try:
        credential = Credential()
        with managed_auth(workspace.resolve(), credential, config.tenant_id, frozenset({AUTH_PROBE_SCOPE})) as env:
            path = env["AZD_AUTH_ENDPOINT"].removeprefix("unix:")
            for fail, key, scope, expected in (
                (False, env["AZD_AUTH_KEY"], AUTH_PROBE_SCOPE, 200),
                (True, env["AZD_AUTH_KEY"], AUTH_PROBE_SCOPE, 200),
                (False, "wrong", AUTH_PROBE_SCOPE, 403),
                (False, env["AZD_AUTH_KEY"], "https://evil.example/.default", 403),
            ):
                credential.fail = fail
                connection = UnixConnection(path)
                connection.request(
                    "POST", "/token?api-version=2023-07-12-preview",
                    body=json.dumps({"scopes": [scope], "tenantId": config.tenant_id}),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                body = json.loads(response.read())
                assert response.status == expected
                assert "SDK-SECRET" not in json.dumps(body)
                if fail:
                    assert body["status"] == "error" and body["code"] == "GetTokenError"
                elif expected == 200:
                    assert env.confirms_token(body["token"], 2000000000, AUTH_PROBE_SCOPE)
                connection.close()
        assert env._issued == {}
    finally:
        import shutil
        shutil.rmtree(workspace)
