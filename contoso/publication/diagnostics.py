"""Bounded in-memory CLI observation; only fixed codes and boolean facts escape."""

from dataclasses import dataclass
from datetime import datetime
import errno
import json
import os
import re
import signal
import subprocess
import threading
import time

from .auth import AUTH_PROBE_SCOPE

MAX_STREAM_BYTES = 131072
MAX_SECONDS = 210
_CODE_MAP = {
    "AuthenticationFailed": "azd_authentication_failed",
    "NotLoggedIn": "azd_not_logged_in",
    "NoCurrentUser": "azd_not_logged_in",
    "CredentialUnavailable": "azd_credential_unavailable",
    "CredentialUnavailableError": "azd_credential_unavailable",
    "GetTokenError": "azd_credential_unavailable",
    "PermissionDenied": "azd_executable_permission_denied",
    "ExecFormatError": "azd_executable_format_invalid",
    "ExtensionNotInstalled": "azd_extension_missing",
    "ExtensionNotFound": "azd_extension_missing",
    "UnknownFlag": "azd_invalid_flags",
    "InvalidTemplate": "azd_schema_invalid",
    "SchemaValidationError": "azd_schema_invalid",
    "InvalidYaml": "azd_schema_invalid",
    "EnvironmentNotFound": "azd_environment_binding_invalid",
    "MissingEnvironmentVariable": "azd_environment_binding_invalid",
    "RoleAssignmentsWriteForbidden": "azd_role_assignment_write_forbidden",
    "AuthorizationPermissionMismatch": "azd_storage_permission_mismatch",
    "ForbiddenByRbac": "azd_resource_rbac_forbidden",
    "ForbiddenByFirewall": "azd_resource_firewall_forbidden",
    "ForbiddenByConnection": "azd_resource_connection_forbidden",
    "PublicNetworkAccessDisabled": "azd_private_network_failure",
    "PrivateEndpointConnectionError": "azd_private_network_failure",
}
_CODE_MAP.update({
    code: "azd_" + code for code in (
        "invalid_agent_manifest", "invalid_service_config", "invalid_environment_variable_name",
        "agent_definition_not_found", "missing_published_container_artifact",
        "missing_ai_project_endpoint", "missing_ai_project_id", "missing_azure_subscription_id",
        "missing_azure_tenant_id", "environment_values_failed", "project_not_found",
        "tenant_lookup_failed", "principal_lookup_failed", "credential_creation_failed",
        "private_acr_network_access_failed", "acr_permission_denied",
        "developer_missing_ai_user_role", "developer_missing_role_assign_write_role",
        "developer_missing_acr_role", "agent_identity_rbac_failed",
        "incompatible_azd_version", "incomplete_agent_protocol_metadata",
        "azd_client_failed", "agent_create_failed", "container_start_failed", "container_start_timeout",
    )
})
_TEXT_RULES = (
    *((code, "(?i)" + re.escape(action), r"(?i)(?:AuthorizationFailed|forbidden|not authorized|403)")
      for action, code in (
          ("Microsoft.Resources/subscriptions/read", "azd_subscription_read_forbidden"),
          ("Microsoft.Resources/subscriptions/resourceGroups/read", "azd_resource_group_read_forbidden"),
          ("Microsoft.CognitiveServices/accounts/read", "azd_foundry_account_read_forbidden"),
          ("Microsoft.CognitiveServices/accounts/projects/read", "azd_project_read_forbidden"),
          ("Microsoft.ContainerRegistry/registries/read", "azd_registry_read_forbidden"),
          ("Microsoft.Authorization/roleAssignments/read", "azd_role_assignment_read_forbidden"),
          ("Microsoft.Authorization/roleDefinitions/read", "azd_role_definition_read_forbidden"),
          ("Microsoft.Authorization/permissions/read", "azd_permission_read_forbidden"),
          ("Microsoft.Resources/subscriptions/locations/read", "azd_location_read_forbidden"),
          ("Microsoft.ResourceGraph/resources/read", "azd_resource_graph_read_forbidden"),
      )),
    *((code, pattern, r"(?i)(?:AuthorizationFailed|forbidden|not authorized|403)")
      for pattern, code in (
          (r"(?i)management\.azure\.com", "azd_management_endpoint_forbidden"),
          (r"(?i)graph\.microsoft\.com", "azd_directory_endpoint_forbidden"),
          (r"(?i)\.services\.ai\.azure\.com", "azd_foundry_endpoint_forbidden"),
          (r"(?i)\.blob\.core\.windows\.net", "azd_blob_endpoint_forbidden"),
          (r"(?i)\.azurecr\.io", "azd_registry_endpoint_forbidden"),
      )),
    ("azd_role_assignment_write_forbidden",
     r"(?i)(?:Microsoft\.Authorization/roleAssignments/write|roleAssignmentsWriteForbidden)",
     r"(?i)(?:AuthorizationFailed|forbidden|not authorized|does not have authorization|403)"),
    ("azd_private_network_failure",
     r"(?i)(?:PublicNetworkAccessDisabled|public (?:network )?access is disabled|"
     r"private endpoint.{0,120}(?:timeout|failed|forbidden)|dial tcp.{0,120}i/o timeout)", None),
    ("azd_executable_format_invalid", r"(?i)(?:exec format error|cannot execute binary file)", None),
    ("azd_executable_permission_denied",
     r"(?i)(?:fork/exec|cannot execute|executable).{0,120}permission denied", None),
    ("azd_executable_unavailable",
     r"(?i)(?:executable file not found|fork/exec.{0,120}no such file|"
     r"Azure Developer CLI.{0,80}(?:not found|could not be found|not installed)|"
     r"azd:\s*(?:command )?not found)", None),
    ("azd_disk_full", r"(?i)(?:no space left on device|disk quota exceeded)", None),
    ("azd_extension_rpc_unavailable",
     r"(?i)(?:rpc error:\s*code\s*=\s*(?:Unimplemented|Unavailable)|"
     r"failed to (?:start|connect to|register).{0,60}gRPC)", None),
    ("azd_container_tool_unavailable",
     r"(?i)(?:\bdocker\b.{0,80}(?:not installed|not found|daemon is not running)|"
     r"cannot connect to the docker daemon)", None),
    ("azd_package_source_missing",
     r"(?i)(?:(?:Dockerfile|agent definition).{0,80}(?:no such file|not found|does not exist))", None),
    ("azd_extension_missing",
     r"(?i)(?:extension.{0,120}(?:not installed|not found|missing)|"
     r"install the required extension|no extensions installed)", None),
    ("azd_invalid_flags", r"(?i)(?:unknown (?:flag|command)|unrecognized (?:flag|argument))", None),
    ("azd_environment_binding_invalid",
     r"(?i)(?:environment.{0,120}(?:not found|does not exist)|"
     r"(?:missing|required|undefined|not set).{0,100}environment (?:variable|value)|"
     r"environment (?:variable|value).{0,100}(?:missing|required|undefined|not set)|"
     r"failed.{0,80}(?:substitut|interpolat))", None),
    ("azd_schema_invalid",
     r"(?i)(?:invalid (?:yaml|template|schema)|schema validation|"
     r"(?:unmarshal|parse|parsing).{0,100}(?:azure\.yaml|yaml))", None),
    ("azd_auth_principal_unavailable",
     r"(?i)(?:external auth token did not contain a usable account identifier|"
     r"(?:missing|no).{0,50}(?:upn|unique_name|preferred_username)|"
     r"(?:could not|cannot|unable to).{0,70}(?:user ?name|user principal)|"
     r"fetching current user information:)", None),
    ("azd_not_logged_in", r"(?i)(?:not logged in|not signed in|no current user|run [`'\"]?azd auth login)", None),
    ("azd_credential_unavailable",
     r"(?i)(?:CredentialUnavailable|GetTokenError|managed identity token acquisition failed|"
     r"(?:missing|no|unavailable).{0,50}credential)", None),
    ("azd_authentication_failed", r"(?i)(?:AuthenticationFailed|authentication failed)", None),
    ("azd_authorization_failed",
     r"(?i)(?:\bAuthorizationFailed\b|\bForbidden\b|status(?:\s*code)?\s*[:=]\s*403\b)", None),
)
SAFE_ERROR_CODES = (
    frozenset(_CODE_MAP.values()) | frozenset(code for code, _, _ in _TEXT_RULES)
    | {"azd_unclassified_error", "azd_process_killed", "azd_process_aborted"}
    | {"azd_external_credential_request_rejected"}
)
_GENERIC_FAILURE_CODES = frozenset({
    "azd_authorization_failed", "azd_management_endpoint_forbidden",
    "azd_directory_endpoint_forbidden", "azd_foundry_endpoint_forbidden",
    "azd_blob_endpoint_forbidden", "azd_registry_endpoint_forbidden",
})
_ENVIRONMENT_NAMES = frozenset({
    "AZURE_ENV_NAME", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP",
    "AZURE_AI_PROJECT_ID", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_CONTAINER_REGISTRY_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_CONTAINER_REGISTRY_NAME", "AZURE_CONTAINER_REGISTRY_RESOURCE_ID",
    "AZURE_LOCATION", "AZURE_AI_MODEL_DEPLOYMENT_NAME",
})


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    error_code: str | None = None
    version_valid: bool = False
    environment_bound: bool = False
    auth_authenticated: bool = False
    token_credential_valid: bool = False

    def __post_init__(self):
        if (type(self.returncode) is not int or self.error_code is not None and self.error_code not in SAFE_ERROR_CODES
                or any(type(value) is not bool for value in (
                    self.version_valid, self.environment_bound, self.auth_authenticated, self.token_credential_valid,
                ))):
            raise ValueError("Only fixed codes and boolean command facts are permitted")


class CommandEnvironment(dict):
    """Observation is an in-process attribute, never an OS environment entry."""

    def __init__(self, values, credential_observation=None):
        super().__init__(values)
        self.credential_observation = credential_observation


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous CLI event")
        result[key] = value
    return result


def _json(text):
    try:
        return json.loads(text, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None


def _error_code(event):
    if not isinstance(event, dict):
        return None
    error = event.get("error")
    if isinstance(error, dict):
        for _ in range(8):
            if isinstance(error.get("code"), str) and error["code"] in _CODE_MAP:
                return _CODE_MAP[error["code"]]
            error = error.get("innererror")
            if not isinstance(error, dict):
                break
    if event.get("type") == "error" or event.get("status") == "error":
        return _CODE_MAP.get(event.get("code")) if isinstance(event.get("code"), str) else None
    return None


def _has_error(event):
    return isinstance(event, dict) and (
        bool(event.get("error")) or event.get("type") == "error" or event.get("status") == "error"
    )


def _events(decoded):
    event = _json(decoded)
    if event is not None:
        return (event,)
    return tuple(
        _json(line) for line in decoded.splitlines()[-256:] if line.lstrip().startswith("{")
    )


def classify_azd_failure(stdout, stderr):
    decoded_streams = []
    for stream in (stderr, stdout):
        decoded = bytes(stream[-MAX_STREAM_BYTES:]).decode("utf-8", errors="replace")
        event = _json(decoded)
        if (isinstance(event, dict) and event.get("status") in ("success", "authenticated", "passed")
                and not _has_error(event)):
            continue
        decoded_streams.append(decoded)
    text = "\n".join(decoded_streams)
    for code, pattern, requirement in _TEXT_RULES:
        if code in _GENERIC_FAILURE_CODES:
            continue
        if re.search(pattern, text) and (requirement is None or re.search(requirement, text)):
            return code
    for decoded in decoded_streams:
        for event in _events(decoded):
            code = _error_code(event)
            if code:
                return code
    for code, safe in _CODE_MAP.items():
        if re.search(r"(?<!\w)" + re.escape(code) + r"(?!\w)", text):
            return safe
    for code, pattern, requirement in _TEXT_RULES:
        if (code in _GENERIC_FAILURE_CODES and re.search(pattern, text)
                and (requirement is None or re.search(requirement, text))):
            return code
    return None


def _facts(argv, env, stdout, complete):
    if not complete:
        return {}
    data = _json(stdout.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        return {}
    if argv[1:2] == ["version"]:
        version = data.get("azd", {}).get("version") if isinstance(data.get("azd"), dict) else None
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", version):
            return {"version_valid": tuple(map(int, version.split("."))) >= (1, 37, 0)}
    elif argv[1:3] == ["env", "get-values"]:
        expected = {key: value for key, value in env.items() if key.startswith("CONTOSO_") or key in _ENVIRONMENT_NAMES}
        return {"environment_bound": bool(expected) and data == expected}
    elif argv[1:3] == ["auth", "status"]:
        return {"auth_authenticated": data.get("status") == "authenticated"}
    elif argv[1:3] == ["auth", "token"]:
        token, expiration = data.get("token"), data.get("expiresOn")
        observer = getattr(env, "credential_observation", None)
        if (not isinstance(token, str) or not token or not isinstance(expiration, str)
                or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})",
                    expiration,
                ) or observer is None):
            return {}
        try:
            expiry = datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return {}
        return {"token_credential_valid": (
            expiry > time.time() + 5 and observer.confirms_token(token, expiry, AUTH_PROBE_SCOPE)
        )}
    return {}


class _Stream:
    def __init__(self):
        self.bytes = bytearray()
        self.truncated = False
        self.failed = False

    def read(self, pipe):
        try:
            while chunk := pipe.read(4096):
                self.bytes.extend(chunk)
                if len(self.bytes) > MAX_STREAM_BYTES:
                    del self.bytes[:-MAX_STREAM_BYTES]
                    self.truncated = True
        except (OSError, ValueError):
            self.failed = True


def bounded_command(argv, *, cwd, env, timeout):
    from .executor import PublicationFailure

    deadline = time.monotonic() + min(timeout, MAX_SECONDS)
    execution_deadline = deadline - min(5, min(timeout, MAX_SECONDS) / 4)
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix",
        )
    except OSError as error:
        code = {
            errno.EACCES: "azd_executable_permission_denied",
            errno.ENOEXEC: "azd_executable_format_invalid",
            errno.ENOENT: "azd_executable_unavailable",
        }.get(error.errno, "deployment_process_unavailable")
        raise PublicationFailure(code) from None
    except subprocess.TimeoutExpired:
        raise PublicationFailure("deployment_timeout") from None
    streams = (_Stream(), _Stream())
    pipes = (process.stdout, process.stderr)
    threads = [
        threading.Thread(target=stream.read, args=(pipe,), daemon=True)
        for stream, pipe in zip(streams, pipes)
    ]
    for thread in threads:
        thread.start()
    try:
        code = process.wait(timeout=max(0, execution_deadline - time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0, execution_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired("fixed-azd", timeout)
        if any(stream.failed for stream in streams):
            raise PublicationFailure("azd_output_unavailable")
        stdout, stderr = (stream.bytes for stream in streams)
        facts = _facts(argv, env, stdout, not streams[0].truncated)
        token_command = argv[1:3] == ["auth", "token"]
        token_event = _json(bytes(stdout).decode("utf-8", errors="replace")) if token_command else None
        token_error = _error_code(token_event)
        token_failure = _has_error(token_event)
        token_event = None
        if token_command:
            # Never run opaque token output through diagnostic text matching.
            stdout.clear()
        events = tuple(
            event for stream in (stderr, stdout)
            for event in _events(bytes(stream).decode("utf-8", errors="replace"))
        )
        if code != 0:
            error = classify_azd_failure(stdout, stderr)
            if error is None:
                error = {
                    -9: "azd_process_killed", 137: "azd_process_killed",
                    -6: "azd_process_aborted", 134: "azd_process_aborted",
                }.get(code)
        else:
            error = next((matched for event in events if (matched := _error_code(event))), None)
        error = error or token_error
        observer = getattr(env, "credential_observation", None)
        if code != 0 and getattr(observer, "credential_rejected", False):
            error = "azd_external_credential_request_rejected"
        if error is None and (token_failure or any(_has_error(event) for event in events)):
            error = "azd_unclassified_error"
        return CommandResult(code, error, **facts)
    except subprocess.TimeoutExpired:
        raise PublicationFailure("deployment_timeout") from None
    finally:
        # Only the process group started by this invocation is terminated.
        if process.poll() is None or any(thread.is_alive() for thread in threads):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise PublicationFailure("deployment_timeout") from None
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        for pipe in pipes:
            pipe.close()
        for stream in streams:
            stream.bytes.clear()
