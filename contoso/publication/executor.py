"""Fixed-target publication from the existing VNet-integrated Linux Gateway."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import yaml

from .auth import AUTH_PROBE_SCOPE, managed_auth
from .diagnostics import CommandEnvironment, CommandResult, bounded_command


DOC_NAMES = frozenset({"knowledge-returnables.md", "supplier-delivery-terms.md"})
SERVICE_NAMES = frozenset({"contoso-knowledge-agent", "contoso-foundry-sdk-agent", "contoso-foundry-agent"})
MAX_SECONDS = 210
_VERSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SELECTOR_VERSION = re.compile(r"[A-Za-z0-9_@$*:.()~<>=+|-]{1,128}")
_RFC1918 = tuple(ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_CONTOSO_BINDINGS = frozenset({
    "CONTOSO_ENTRA_TENANT_ID", "CONTOSO_ENTRA_API_CLIENT_ID", "CONTOSO_STUDIO_ENVIRONMENT_ID",
    "CONTOSO_STUDIO_STANDARD_SCHEMA", "CONTOSO_STUDIO_HARNESS_ENDPOINT", "CONTOSO_KNOWLEDGE_A2A_ENDPOINT",
    "CONTOSO_GATEWAY_URL", "CONTOSO_SOURCE_BLOB_ENDPOINT", "CONTOSO_KNOWLEDGE_SOURCE_SHA256",
    "CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS", "CONTOSO_ROUTER_PLATFORM_CALLER_IDS",
    "CONTOSO_KNOWLEDGE_RUNTIME_ROLE", "CONTOSO_ROUTER_RUNTIME_ROLE",
})
_EXTENSION_IDS = frozenset({
    "microsoft.foundry", "azure.ai.agents", "azure.ai.projects", "azure.ai.inspector",
    "azure.ai.connections", "azure.ai.routines", "azure.ai.skills", "azure.ai.toolboxes",
})
_AUTH_SCOPES = frozenset({
    AUTH_PROBE_SCOPE,
    "https://management.azure.com/.default", "https://management.azure.com//.default",
    "https://ai.azure.com/.default", "https://cognitiveservices.azure.com/.default",
    "https://containerregistry.azure.net/.default", "https://storage.azure.com/.default",
})


class PublicationFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA256 pin")


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise ValueError("Invalid approved name")


def _schema_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ValueError("Invalid explicit STUDIO schema")


def _https(value, suffix):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(suffix) or parsed.port is not None
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Expected an approved Azure HTTPS endpoint")
    return parsed


def _local(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Only package-relative paths are permitted")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path escapes approved package")
    return resolved


def _extension_path(identifier):
    return Path("extensions") / identifier / (identifier.replace(".", "-") + "-linux-amd64")


_EXTENSION_EXECUTABLES = frozenset(
    Path("azd-config") / _extension_path(identifier)
    for identifier in _EXTENSION_IDS - {"microsoft.foundry"}
)


def _owned_directory(root, directory):
    current = root
    for part in directory.relative_to(root).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


def _copy_pinned(root, relative, source, pin, *, executable=False, error_code):
    if _digest(source) != pin:
        raise PublicationFailure(error_code)
    target = _local(root, relative)
    _owned_directory(root, target.parent)
    shutil.copyfile(source, target)
    if _digest(target) != pin:
        raise PublicationFailure(error_code)
    target.chmod(0o755 if executable else 0o644)
    return target


def _verify_executables(pins):
    for path, pin in pins.items():
        if _digest(path) != pin:
            raise PublicationFailure("executable_hash_mismatch")
        path.chmod(0o755)


def _validate_installed_paths(root, pins):
    config = root / "azd-config" / "config.json"
    if not config.exists():
        return
    installed = json.loads(config.read_text(encoding="utf-8")).get("extension", {}).get("installed", {})
    if not isinstance(installed, dict) or set(installed) - _EXTENSION_IDS:
        raise PublicationFailure("extension_executable_path_invalid")
    for identifier, entry in installed.items():
        if not isinstance(entry, dict):
            raise PublicationFailure("extension_executable_path_invalid")
        relative = entry.get("path")
        if identifier == "microsoft.foundry":
            if relative not in ("", None):
                raise PublicationFailure("extension_executable_path_invalid")
            continue
        if (not isinstance(relative, str) or Path(relative) != _extension_path(identifier)
                or Path("azd-config") / Path(relative) not in pins):
            raise PublicationFailure("extension_executable_path_invalid")


def _runtime_url(value, suffix, *, query=False, https_port=False):
    parsed = urlsplit(value)
    if (not isinstance(value, str) or len(value) > 2048 or parsed.scheme != "https"
            or not parsed.hostname or not parsed.hostname.endswith(suffix)
            or parsed.port not in ((None, 443) if https_port else (None,))
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(part in (".", "..") or "%" in part for part in parsed.path.split("/"))
            or parsed.query and not (
                query and re.fullmatch(r"api-version=(?:v1|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?)", parsed.query)
            )):
        raise ValueError("Invalid nonsecret runtime HTTPS endpoint")
    return parsed


def _caller_ids(value, *, probe):
    if value == "" and probe:
        return
    callers = json.loads(value)
    if (not isinstance(callers, list) or len(callers) > 16 or not callers and not probe
            or any(not isinstance(item, str) or not 0 < len(item) <= 512 or not item.strip()
                   or any(ord(c) < 32 or ord(c) > 126 for c in item) for item in callers)
            or len(set(callers)) != len(callers)):
        raise ValueError("Invalid explicit platform caller JSON allowlist")


def _dotenv_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _user_agent(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 256
            or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise ValueError("A bounded inline operator process user-agent is required")


def _field(value, name):
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _record_identity(value):
    if value is None:
        return None
    result = {}
    for key in ("principal_id", "client_id"):
        identifier = _field(value, key)
        if identifier is not None:
            UUID(identifier)
        result[key] = identifier
    status = _field(value, "status")
    if isinstance(status, str):
        status = status.lower()
    if status not in (None, "active", "disabled"):
        raise PublicationFailure("agent_record_identity_status_invalid")
    result["status"] = status
    return result


def _record_endpoint(value, project_endpoint):
    if value is None:
        return None
    result = {}
    project = urlsplit(project_endpoint)
    if isinstance(value, str):
        endpoint = _runtime_url(value, ".services.ai.azure.com", query=True)
        if endpoint.hostname != project.hostname or not endpoint.path.startswith(project.path + "/"):
            raise PublicationFailure("agent_record_selector_invalid")
        return value
    selector = _field(value, "version_selector")
    if selector is not None:
        rules = _field(selector, "version_selection_rules")
        if not isinstance(rules, list) or len(rules) > 16:
            raise PublicationFailure("agent_record_invalid")
        projected = []
        for rule in rules:
            kind, version = _field(rule, "type"), _field(rule, "agent_version")
            if not isinstance(kind, str) or kind.lower() != "fixedratio":
                raise PublicationFailure("agent_record_selector_kind_invalid")
            if version is None:
                raise PublicationFailure("agent_record_selector_version_missing")
            if not isinstance(version, str):
                raise PublicationFailure("agent_record_selector_version_type_invalid")
            if version == "":
                raise PublicationFailure("agent_record_selector_version_empty")
            # SDK selector expressions are not concrete version IDs or URL segments.
            if not _SELECTOR_VERSION.fullmatch(version):
                raise PublicationFailure("agent_record_selector_version_invalid")
            kind = "FixedRatio"
            projected.append({"type": kind, "agent_version": version})
        result["version_selector"] = {"version_selection_rules": projected}
    protocols = _field(value, "protocol_configuration")
    if protocols is not None:
        selected = {}
        for protocol in ("responses", "a2a", "invocations", "invocations_ws", "activity", "mcp"):
            configuration = _field(protocols, protocol)
            if configuration is not None:
                selected[protocol] = {}
                endpoint = _field(configuration, "url")
                if endpoint is not None:
                    selected[protocol]["url"] = _record_endpoint(endpoint, project_endpoint)
        result["protocol_configuration"] = selected
    # Never serialize authorization_schemes, headers or an entire SDK object.
    return result


def _agent_record(agent, name, project_endpoint, *, include_endpoint=True):
    if agent is None:
        return {
            "name": name, "version": None, "status": "not_found",
            "instance_identity": None, "agent_endpoint": None,
        }
    if _field(agent, "name") != name:
        raise PublicationFailure("agent_record_name_mismatch")
    latest = _field(_field(agent, "versions"), "latest")
    version = _field(latest, "version")
    status = _field(latest, "status")
    if isinstance(status, str):
        status = status.lower()
    if version is not None and (not isinstance(version, str) or not _VERSION_ID.fullmatch(version)):
        raise PublicationFailure("agent_record_version_invalid")
    if status not in (None, "creating", "active", "failed", "deleting", "deleted"):
        raise PublicationFailure("agent_record_status_invalid")
    if status == "active" and version is None:
        raise PublicationFailure("agent_record_active_version_missing")
    identity = _field(agent, "instance_identity")
    if identity is None:
        identity = _field(latest, "instance_identity")
    if version is None and status is None:
        return {
            "name": name, "version": None, "status": "not_deployed",
            "instance_identity": _record_identity(identity), "agent_endpoint": None,
        }
    return {
        "name": name, "version": version, "status": status,
        "instance_identity": _record_identity(identity),
        "agent_endpoint": (
            _record_endpoint(_field(agent, "agent_endpoint"), project_endpoint)
            if include_endpoint else None
        ),
    }


@dataclass(frozen=True)
class ExtensionSetup:
    """Operator-staged official Linux artifacts; the worker never downloads them."""

    registry_relative_path: str
    registry_sha256: str
    artifacts: tuple[tuple[str, str], ...]
    versions: tuple[tuple[str, str], ...]

    def __post_init__(self):
        _pin(self.registry_sha256)
        if (set(dict(self.versions)) != _EXTENSION_IDS or len(self.versions) != len(_EXTENSION_IDS)
                or any(not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-beta\.[0-9]+", v)
                       for _, v in self.versions)):
            raise ValueError("Exact versions for the fixed Foundry extension graph required")
        if len(self.artifacts) != 7 or len(dict(self.artifacts)) != 7:
            raise ValueError("Seven pinned concrete Linux extension archives required")
        for relative, pin in self.artifacts:
            _pin(pin)
            if not relative.endswith(".tar.gz"):
                raise ValueError("Only staged Linux tar.gz extension archives supported")


@dataclass(frozen=True)
class Document:
    name: str
    relative_path: str
    sha256: str
    expected_etag: str | None = None

    def __post_init__(self):
        if self.name not in DOC_NAMES:
            raise ValueError("Unapproved source document")
        _pin(self.sha256)
        if self.expected_etag is not None and not re.fullmatch(r'"[A-Za-z0-9-]{1,128}"', self.expected_etag):
            raise ValueError("Invalid source ETag")


@dataclass(frozen=True)
class OperatorConfig:
    package_root: Path
    workspace_root: Path
    azure_yaml_sha256: str
    azd_relative_path: str
    azd_sha256: str
    extension_files: tuple[tuple[str, str], ...]
    services: tuple[str, str, str]
    image: str
    tenant_id: str
    subscription_id: str
    resource_group: str
    project_id: str
    project_endpoint: str
    registry_endpoint: str
    registry_id: str
    blob_endpoint: str
    source_storage_id: str
    environment: str
    documents: tuple[Document, Document]
    environment_values: tuple[tuple[str, str], ...] = ()
    extension_setup: ExtensionSetup | None = None

    def __post_init__(self):
        if not isinstance(self.package_root, Path) or not isinstance(self.workspace_root, Path):
            raise ValueError("Operator paths must be pathlib Paths")
        _pin(self.azure_yaml_sha256)
        _pin(self.azd_sha256)
        if len(self.services) != 3 or set(self.services) != SERVICE_NAMES:
            raise ValueError("Exactly three selected hosted services required")
        for value in (*self.services, self.environment, self.resource_group):
            _name(value)
        for value in (self.tenant_id, self.subscription_id):
            UUID(value)
        project = _https(self.project_endpoint, ".services.ai.azure.com")
        if not re.fullmatch(r"/api/projects/[a-zA-Z0-9_-]+", project.path):
            raise ValueError("Invalid Foundry project endpoint")
        registry = _https(self.registry_endpoint, ".azurecr.io")
        blob = _https(self.blob_endpoint, ".blob.core.windows.net")
        if registry.path not in ("", "/") or blob.path not in ("", "/"):
            raise ValueError("Invalid Azure service endpoint path")
        prefix = f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}/providers/"
        if (not self.project_id.startswith(prefix + "Microsoft.CognitiveServices/accounts/")
                or not re.fullmatch(re.escape(prefix) + r"Microsoft.CognitiveServices/accounts/[a-zA-Z0-9_-]+/projects/[a-zA-Z0-9_-]+", self.project_id)
                or self.project_id.rsplit("/", 1)[-1] != project.path.rsplit("/", 1)[-1]):
            raise ValueError("Project ARM ID must match configured subscription, group and project")
        if not re.fullmatch(re.escape(prefix) + r"Microsoft.ContainerRegistry/registries/[a-zA-Z0-9]+", self.registry_id):
            raise ValueError("Invalid registry ARM ID")
        if registry.hostname.split(".")[0] != self.registry_id.rsplit("/", 1)[-1]:
            raise ValueError("Registry endpoint and ID differ")
        if (not re.fullmatch(re.escape(prefix) + r"Microsoft.Storage/storageAccounts/[a-z0-9]+", self.source_storage_id)
                or blob.hostname.split(".")[0] != self.source_storage_id.rsplit("/", 1)[-1]):
            raise ValueError("Source Storage ARM ID and endpoint differ")
        if not re.fullmatch(re.escape(registry.hostname) + r"/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}", self.image):
            raise ValueError("Only digest-pinned approved ACR images permitted")
        if len(self.documents) != 2 or {d.name for d in self.documents} != DOC_NAMES:
            raise ValueError("Exactly two approved documents required")
        for doc in self.documents:
            _local(self.package_root, doc.relative_path)
        _local(self.package_root, self.azd_relative_path)
        if ((not self.extension_files and self.extension_setup is None)
                or self.extension_files and self.extension_setup is not None
                or len(dict(self.extension_files)) != len(self.extension_files)):
            raise ValueError("Pinned extension package files required")
        for relative, pin in self.extension_files:
            _pin(pin)
            _local(self.package_root, relative)
            if (not Path(relative).parts or Path(relative).parts[0] != "azd-config"
                    or any(part.startswith("auth") for part in Path(relative).parts)
                    or Path(relative).name != "config.json" and "extensions" not in Path(relative).parts):
                raise ValueError("Only clean extension configuration/artifacts permitted")
        if self.extension_setup is not None:
            _local(self.package_root, self.extension_setup.registry_relative_path)
            for relative, _ in self.extension_setup.artifacts:
                _local(self.package_root, relative)
        extras = dict(self.environment_values)
        if (len(extras) != len(self.environment_values)
                or set(extras) - (_CONTOSO_BINDINGS | {
                    "CONTOSO_ROUTER_SCOPE", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "AZURE_LOCATION",
                })
                or not _CONTOSO_BINDINGS.issubset(extras)
                or any(not isinstance(value, str) for value in extras.values())):
            raise ValueError("Unapproved deployment environment values")
        roles = (extras["CONTOSO_KNOWLEDGE_RUNTIME_ROLE"], extras["CONTOSO_ROUTER_RUNTIME_ROLE"])
        knowledge_probe = roles[0] == "identity-probe"
        router_probe = roles[1] == "identity-probe"
        router_link = roles[1] == "identity-link-probe"
        if (roles[0] not in {"identity-probe", "knowledge"}
                or roles[1] not in {"identity-probe", "identity-link-probe", "router"}):
            raise ValueError("Explicit supported knowledge and router runtime roles required")
        bootstrap = knowledge_probe and (router_probe or router_link)
        router_scope = extras.get("CONTOSO_ROUTER_SCOPE", "catalog")
        if router_scope not in {"catalog", "studio-harness"}:
            raise ValueError("Explicit supported Router scope required")
        if UUID(extras["CONTOSO_ENTRA_TENANT_ID"]) != UUID(self.tenant_id):
            raise ValueError("Runtime tenant must match the selected project tenant")
        UUID(extras["CONTOSO_ENTRA_API_CLIENT_ID"])
        provider_keys = {
            "CONTOSO_STUDIO_ENVIRONMENT_ID", "CONTOSO_STUDIO_STANDARD_SCHEMA", "CONTOSO_STUDIO_HARNESS_ENDPOINT",
            "CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "CONTOSO_GATEWAY_URL",
        }
        for key in provider_keys:
            value = extras[key]
            if (value == "" and key == "CONTOSO_STUDIO_STANDARD_SCHEMA"
                    and router_scope == "studio-harness"):
                continue
            if value == "" and bootstrap and not (router_link and key == "CONTOSO_KNOWLEDGE_A2A_ENDPOINT"):
                continue
            if key == "CONTOSO_STUDIO_ENVIRONMENT_ID":
                UUID(value)
            elif key == "CONTOSO_STUDIO_STANDARD_SCHEMA":
                _schema_identifier(value)
            elif key == "CONTOSO_STUDIO_HARNESS_ENDPOINT":
                endpoint = _runtime_url(value, ".powerplatform.com", https_port=True)
                if (endpoint.hostname != "api.powerplatform.com"
                        or not re.fullmatch(
                            r"/copilotstudio-a2a/(?:[A-Za-z0-9_.-]+/)+agents/[A-Za-z0-9_.-]+",
                            endpoint.path,
                        )):
                    raise ValueError("Invalid native STUDIO Harness A2A endpoint")
            elif key == "CONTOSO_GATEWAY_URL":
                endpoint = _runtime_url(value, ".azurewebsites.net")
                if endpoint.path not in ("", "/"):
                    raise ValueError("Gateway URL must identify the Gateway origin")
            else:
                endpoint = _runtime_url(value, ".services.ai.azure.com", query=True, https_port=True)
                if (endpoint.hostname != project.hostname
                        or not endpoint.path.startswith(project.path + "/agents/contoso-knowledge-agent/")):
                    raise ValueError("Knowledge endpoint must belong to the selected project and agent")
        if extras["CONTOSO_SOURCE_BLOB_ENDPOINT"].rstrip("/") != self.blob_endpoint.rstrip("/"):
            raise ValueError("Runtime source endpoint must match the approved source Storage")
        _pin(extras["CONTOSO_KNOWLEDGE_SOURCE_SHA256"])
        if extras["CONTOSO_KNOWLEDGE_SOURCE_SHA256"] != next(d.sha256 for d in self.documents if d.name == "knowledge-returnables.md"):
            raise ValueError("Runtime source hash must match the approved knowledge document")
        _caller_ids(extras["CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS"], probe=knowledge_probe)
        _caller_ids(extras["CONTOSO_ROUTER_PLATFORM_CALLER_IDS"], probe=router_probe)
        for key in set(extras) - _CONTOSO_BINDINGS:
            _name(extras[key])

    @property
    def identity_probe(self):
        values = dict(self.environment_values)
        return (values["CONTOSO_KNOWLEDGE_RUNTIME_ROLE"], values["CONTOSO_ROUTER_RUNTIME_ROLE"]) == (
            "identity-probe", "identity-probe",
        )

    def values(self):
        return {
            "AZURE_ENV_NAME": self.environment,
            "AZURE_TENANT_ID": self.tenant_id,
            "AZURE_SUBSCRIPTION_ID": self.subscription_id,
            "AZURE_RESOURCE_GROUP": self.resource_group,
            "AZURE_AI_PROJECT_ID": self.project_id,
            "AZURE_AI_PROJECT_ENDPOINT": self.project_endpoint,
            "FOUNDRY_PROJECT_ENDPOINT": self.project_endpoint,
            "AZURE_CONTAINER_REGISTRY_ENDPOINT": self.registry_endpoint.removeprefix("https://").rstrip("/"),
            "AZURE_CONTAINER_REGISTRY_NAME": self.registry_id.rsplit("/", 1)[-1],
            "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": self.registry_id,
            "CONTOSO_RUNTIME_IMAGE": self.image,
            "CONTOSO_ROUTER_SCOPE": "catalog",
            **dict(self.environment_values),
        }


def private_dns(hostname, resolver=socket.getaddrinfo):
    try:
        addresses = {entry[4][0] for entry in resolver(hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(
            not any(ipaddress.ip_address(address) in network for network in _RFC1918)
            for address in addresses
        ):
            raise ValueError("Public or non-RFC1918 DNS answer")
        return tuple(sorted(addresses))
    except Exception:
        raise PublicationFailure("private_dns_required") from None


@dataclass
class PublicationJob:
    job_id: str
    status: str = "running"
    services: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    error_code: str | None = None

    def as_dict(self):
        return {
            "job_id": self.job_id, "status": self.status, "services": self.services,
            "sources": self.sources, "error_code": self.error_code,
        }


class PublicationExecutor:
    def __init__(
        self, config: OperatorConfig, *, credential, get_project: Callable,
        list_agents: Callable, blob_factory: Callable, command=bounded_command,
        auth_context=managed_auth, resolver=socket.getaddrinfo,
        job_store: Callable | None = None,
        get_agent: Callable | None = None,
    ):
        self.config = config
        self.credential = credential
        self.get_project = get_project
        self.list_agents = list_agents
        self.blob_factory = blob_factory
        self.command = command
        self.auth_context = auth_context
        self.resolver = resolver
        self.job_store = job_store
        self.get_agent = get_agent

    def _process_env(self, workspace, process_user_agent):
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "SystemRoot", "WINDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        }}
        env.update({
            "HOME": str(workspace), "AZD_CONFIG_DIR": str(workspace / "azd-config"),
            "AZURE_CONFIG_DIR": str(workspace / "unused-az-config"),
            "AZURE_DEV_USER_AGENT": process_user_agent,
            "AZD_NON_INTERACTIVE": "true", "AZD_SUPPRESS_TELEMETRY": "true", "CI": "true",
            "AZD_AGENT_SKIP_ROLE_ASSIGNMENTS": "true",
            **self.config.values(),
        })
        env["PATH"] = os.pathsep.join(filter(None, (str(workspace / "bin"), env.get("PATH"))))
        scratch = workspace / "scratch"
        scratch.mkdir(mode=0o700)
        env.update({key: str(scratch) for key in ("TMPDIR", "TMP", "TEMP")})
        return env

    def _prepared_root(self):
        setup = self.config.extension_setup
        if setup is None:
            raise PublicationFailure("extension_setup_not_configured")
        fingerprint = hashlib.sha256(json.dumps({
            "registry": setup.registry_sha256,
            "artifacts": setup.artifacts, "versions": setup.versions,
        }, sort_keys=True).encode()).hexdigest()
        return self.config.workspace_root / "prepared-extensions" / fingerprint

    def _prepared_files(self):
        root = self._prepared_root()
        try:
            pins = json.loads((root / "file-pins.json").read_text(encoding="utf-8"))
            if not isinstance(pins, dict) or not pins or "config.json" not in pins:
                raise ValueError("Missing extension installation")
            for relative, pin in pins.items():
                _pin(pin)
                path = _local(root / "azd-config", relative)
                if _digest(path) != pin:
                    raise ValueError("Changed prepared extension")
            return root, pins
        except Exception:
            raise PublicationFailure("extensions_not_prepared") from None

    def _prepare_azd(self, workspace):
        return _copy_pinned(
            workspace, Path("bin") / "azd",
            _local(self.config.package_root, self.config.azd_relative_path),
            self.config.azd_sha256, executable=True, error_code="binary_hash_mismatch",
        )

    def _run(self, workspace, executable, pins, env, arguments, *, failure, timeout=MAX_SECONDS,
             credential_observation=None):
        approved = [
            ["version", "--output", "json", "--no-prompt"],
            ["env", "get-values", "--environment", self.config.environment, "--output", "json", "--no-prompt"],
            ["auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", self.config.tenant_id,
             "--output", "json", "--no-prompt"],
        ]
        approved.extend(
            ["deploy", service, "--no-prompt", "--environment", self.config.environment,
             "--timeout", str(MAX_SECONDS)]
            for service in self.config.services
        )
        if self.config.extension_setup is not None:
            approved.append([
                "extension", "install", "microsoft.foundry", "--source",
                str(workspace / "azd-config" / "extensions" / "staged" / "registry.json"),
                "--version", dict(self.config.extension_setup.versions)["microsoft.foundry"], "--no-prompt",
            ])
        if (arguments not in approved or executable != workspace / "bin" / "azd"
                or pins.get(executable) != self.config.azd_sha256):
            raise PublicationFailure("azd_command_not_approved")
        _verify_executables(pins)
        result = self.command(
            [str(executable), *arguments], cwd=workspace,
            env=CommandEnvironment(env, credential_observation), timeout=min(timeout, MAX_SECONDS),
        )
        if type(result) is int:
            result = CommandResult(result)
        if not isinstance(result, CommandResult):
            raise PublicationFailure("azd_command_result_invalid")
        if result.returncode != 0 or result.error_code is not None:
            raise PublicationFailure(result.error_code or failure)
        return result

    def prepare_extensions(self, *, process_user_agent):
        """One explicit offline installer command, never part of service publication."""
        _user_agent(process_user_agent)
        root = self._prepared_root()
        if root.exists():
            _, pins = self._prepared_files()
        else:
            try:
                root.mkdir(parents=True, mode=0o700)
                root.chmod(0o700)
                executable = self._prepare_azd(root)
                env = self._process_env(root, process_user_agent)
                self._install_extensions(root, executable, env)
                config_root = root / "azd-config"
                pins = {
                    str(path.relative_to(config_root)): _digest(path)
                    for path in config_root.rglob("*")
                    if path.is_file() and (
                        path == config_root / "config.json"
                        or path.is_relative_to(config_root / "extensions")
                    )
                }
                _validate_installed_paths(root, {Path("azd-config") / Path(p): pin for p, pin in pins.items()})
                for relative in pins:
                    path = _local(config_root, relative)
                    path.chmod(0o755 if Path("azd-config") / Path(relative) in _EXTENSION_EXECUTABLES else 0o644)
                (root / "file-pins.json").write_text(json.dumps(pins), encoding="utf-8")
            except Exception as error:
                if root.exists():
                    shutil.rmtree(root)
                if isinstance(error, PublicationFailure):
                    raise error
                raise PublicationFailure("extension_preparation_failed") from None
        return {
            "status": "prepared", "versions": dict(self.config.extension_setup.versions),
            "files": pins,
        }

    def _install_extensions(self, workspace, executable, env):
        setup = self.config.extension_setup
        registry_path = _local(self.config.package_root, setup.registry_relative_path)
        if _digest(registry_path) != setup.registry_sha256:
            raise PublicationFailure("extension_registry_hash_mismatch")
        approved = {}
        for relative, pin in setup.artifacts:
            artifact = _local(self.config.package_root, relative)
            if _digest(artifact) != pin:
                raise PublicationFailure("extension_hash_mismatch")
            approved[artifact] = pin
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        entries = registry.get("extensions")
        if (not isinstance(entries, list) or len(entries) != len(_EXTENSION_IDS)
                or {e.get("id") for e in entries} != _EXTENSION_IDS):
            raise PublicationFailure("extension_registry_invalid")
        stage = workspace / "azd-config" / "extensions" / "staged"
        _owned_directory(workspace, stage)
        selected = dict(setup.versions)
        used = set()
        for entry in entries:
            versions = entry.get("versions")
            if (not isinstance(versions, list) or len(versions) != 1
                    or versions[0].get("version") != selected[entry["id"]]):
                raise PublicationFailure("extension_registry_invalid")
            artifacts = versions[0].get("artifacts", {})
            if entry["id"] == "microsoft.foundry":
                if artifacts:
                    raise PublicationFailure("extension_registry_invalid")
                continue
            if set(artifacts) != {"linux/amd64"}:
                raise PublicationFailure("extension_registry_invalid")
            artifact = artifacts["linux/amd64"]
            reference = artifact.get("url")
            if not isinstance(reference, str) or ":" in reference or reference.startswith(("/", "\\")):
                raise PublicationFailure("extension_registry_invalid")
            source = _local(registry_path.parent, reference)
            checksum = artifact.get("checksum", {})
            if (source not in approved or source in used or checksum.get("algorithm") != "sha256"
                    or checksum.get("value") != approved[source]):
                raise PublicationFailure("extension_registry_invalid")
            entrypoint = artifact.get("entryPoint")
            if entrypoint != _extension_path(entry["id"]).name:
                raise PublicationFailure("extension_registry_invalid")
            used.add(source)
            target = _local(stage, reference)
            _copy_pinned(
                workspace, target.relative_to(workspace), source, approved[source],
                error_code="extension_hash_mismatch",
            )
            artifact["url"] = str(target)
        if used != set(approved):
            raise PublicationFailure("extension_registry_invalid")
        local_registry = stage / "registry.json"
        local_registry.write_text(json.dumps(registry), encoding="utf-8")
        self._run(
            workspace, executable, {executable: self.config.azd_sha256}, env,
            ["extension", "install", "microsoft.foundry",
             "--source", str(local_registry), "--version", selected["microsoft.foundry"],
             "--no-prompt"],
            failure="extension_install_nonzero_exit",
        )
        config_root = workspace / "azd-config"
        installed = json.loads((config_root / "config.json").read_text(encoding="utf-8")).get("extension", {}).get("installed", {})
        if set(installed) != _EXTENSION_IDS:
            raise PublicationFailure("extension_install_incomplete")
        for identifier, version in selected.items():
            entry = installed[identifier]
            if entry.get("id") != identifier or entry.get("version") != version:
                raise PublicationFailure("extension_install_incomplete")
            if identifier != "microsoft.foundry":
                path = _local(config_root, entry.get("path", ""))
                if not path.is_file() or path != config_root / _extension_path(identifier):
                    raise PublicationFailure("extension_install_incomplete")
                path.chmod(0o755)

    def _manifest(self):
        config = self.config
        path = _local(config.package_root, "azure.yaml")
        if _digest(path) != config.azure_yaml_sha256:
            raise PublicationFailure("package_hash_mismatch")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {"name", "metadata", "services", "requiredVersions"}:
            raise PublicationFailure("unapproved_manifest")
        services = data.get("services")
        if not isinstance(services, dict) or set(services) != set(config.services):
            raise PublicationFailure("unapproved_manifest")
        for name, service in services.items():
            if (not isinstance(service, dict)
                    or service.get("host") != "azure.ai.agent"
                    or service.get("kind") != "hosted"
                    or service.get("language") != "docker"
                    or service.get("image") not in (config.image, "${CONTOSO_RUNTIME_IMAGE}")
                    or service.get("docker") != {"imagePassthrough": True, "remoteBuild": False}
                    or service.get("name", name) != name
                    or service.get("project", ".") != "."
                    or set(service) - {
                        "host", "kind", "language", "image", "docker", "name",
                        "project", "description", "protocols", "resources", "environment",
                        "metadata", "agentEndpoint", "agentCard", "env", "container",
                    }):
                raise PublicationFailure("unapproved_manifest")
        def deny_hooks(value):
            if isinstance(value, dict):
                if any(k in value for k in ("hooks", "command", "shell", "run", "uses", "codeConfiguration")):
                    raise PublicationFailure("unapproved_manifest")
                for child in value.values():
                    deny_hooks(child)
            elif isinstance(value, list):
                for child in value:
                    deny_hooks(child)
        deny_hooks(data)
        return path.read_bytes()

    def get_agent_records(self, *, include_endpoint=True):
        """Read only the three selected agents, even after a nonzero azd result."""
        if self.get_agent is None:
            raise PublicationFailure("agent_record_adapter_required")
        private_dns(urlsplit(self.config.project_endpoint).hostname, self.resolver)
        records = {}
        try:
            for name in self.config.services:
                agent = self.get_agent(self.config, self.credential, name)
                records[name] = _agent_record(
                    agent, name, self.config.project_endpoint, include_endpoint=include_endpoint,
                )
        except PublicationFailure:
            raise
        except Exception:
            raise PublicationFailure("agent_record_lookup_failed") from None
        return records

    def preflight(self):
        config = self.config
        for endpoint in (config.project_endpoint, config.registry_endpoint, config.blob_endpoint):
            private_dns(urlsplit(endpoint).hostname, self.resolver)
        result = self.get_project(config, self.credential)
        if not isinstance(result, Mapping) or result.get("id", "").lower() != config.project_id.lower():
            raise PublicationFailure("project_identity_mismatch")
        self.list_agents(config, self.credential)

    def publish_sources(self):
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings
        sources = {}
        # Validate both pins before making the first write.
        docs = [(doc, _local(self.config.package_root, doc.relative_path).read_bytes())
                for doc in self.config.documents]
        if any(hashlib.sha256(content).hexdigest() != doc.sha256 for doc, content in docs):
            raise PublicationFailure("source_hash_mismatch")
        private_dns(urlsplit(self.config.blob_endpoint).hostname, self.resolver)
        for doc, content in docs:
            client = self.blob_factory(
                self.config.blob_endpoint, "sources", doc.name, self.credential,
            )
            kwargs = {
                "overwrite": doc.expected_etag is not None,
                "metadata": {"sha256": doc.sha256},
                "content_settings": ContentSettings(content_type="text/markdown; charset=utf-8"),
                "timeout": 30,
            }
            if doc.expected_etag is not None:
                kwargs.update(etag=doc.expected_etag, match_condition=MatchConditions.IfNotModified)
            result = client.upload_blob(content, **kwargs)
            etag = result.get("etag")
            if not isinstance(etag, str) or not etag:
                raise PublicationFailure("source_etag_missing")
            sources[doc.name] = {"sha256": doc.sha256, "etag": etag}
        return sources

    def _outputs(self, workspace, service):
        from dotenv import dotenv_values
        values = dotenv_values(workspace / ".azure" / self.config.environment / ".env", interpolate=False)
        for key, expected in self.config.values().items():
            if values.get(key) != expected:
                raise PublicationFailure("deployment_environment_mismatch")
        prefix = "AGENT_" + service.upper().replace("-", "_") + "_"
        name, version = values.get(prefix + "NAME"), values.get(prefix + "VERSION")
        # The CLI clears disabled protocol endpoints with an explicit empty string.
        endpoints = {
            protocol: values[prefix + protocol + "_ENDPOINT"]
            for protocol in ("RESPONSES", "A2A", "INVOCATIONS", "INVOCATIONS_WS", "ACTIVITY", "MCP")
            if prefix + protocol + "_ENDPOINT" in values
            and values[prefix + protocol + "_ENDPOINT"] != ""
        }
        if name != service or not isinstance(version, str) or not _VERSION_ID.fullmatch(version) or not endpoints:
            raise PublicationFailure("deployment_outputs_missing")
        for protocol, value in endpoints.items():
            candidate = value
            if protocol == "INVOCATIONS_WS" and isinstance(value, str) and value.startswith("wss://"):
                candidate = "https://" + value[len("wss://"):]
            try:
                endpoint = _runtime_url(
                    candidate, ".services.ai.azure.com", query=True, https_port=True,
                )
            except ValueError:
                raise PublicationFailure("deployment_endpoint_mismatch") from None
            project = urlsplit(self.config.project_endpoint)
            if endpoint.hostname != project.hostname or not endpoint.path.startswith(project.path + "/"):
                raise PublicationFailure("deployment_endpoint_mismatch")
            if any(part in (".", "..") or "%" in part for part in endpoint.path.split("/")):
                raise PublicationFailure("deployment_endpoint_mismatch")
        return {"name": name, "version": version, "endpoints": endpoints}

    def publish(self, *, process_user_agent: str) -> PublicationJob:
        return self._publish(self.config.services, process_user_agent=process_user_agent)

    def publish_service(self, service: str, *, process_user_agent: str) -> PublicationJob:
        if not isinstance(service, str) or service not in SERVICE_NAMES:
            raise ValueError("Unapproved publication service")
        return self._publish((service,), process_user_agent=process_user_agent)

    def _prepare_workspace(self, workspace, *, private_preflight):
        config = self.config
        manifest = self._manifest()
        if _digest(_local(config.package_root, config.azd_relative_path)) != config.azd_sha256:
            raise PublicationFailure("binary_hash_mismatch")
        for relative, pin in config.extension_files:
            if _digest(_local(config.package_root, relative)) != pin:
                raise PublicationFailure("extension_hash_mismatch")
        prepared = self._prepared_files() if config.extension_setup is not None else None
        if private_preflight:
            self.preflight()
        workspace.mkdir(parents=True, mode=0o700)
        workspace.chmod(0o700)
        executable = self._prepare_azd(workspace)
        executable_pins = {executable: config.azd_sha256}
        extension_pins = {}
        (workspace / "azure.yaml").write_bytes(manifest)
        state = workspace / ".azure" / config.environment
        _owned_directory(workspace, state)
        (workspace / ".azure" / "config.json").write_text(
            json.dumps({"defaultEnvironment": config.environment}), encoding="utf-8",
        )
        (state / ".env").write_text(
            "".join(f'{k}="{_dotenv_escape(v)}"\n' for k, v in config.values().items()), encoding="utf-8",
        )
        sources = (
            [(_local(config.package_root, relative), Path(relative), pin)
             for relative, pin in config.extension_files]
            if prepared is None else
            [(_local(prepared[0] / "azd-config", relative), Path("azd-config") / Path(relative), pin)
             for relative, pin in prepared[1].items()]
        )
        for source, relative, pin in sources:
            target = _copy_pinned(
                workspace, relative, source, pin,
                executable=relative in _EXTENSION_EXECUTABLES, error_code="extension_hash_mismatch",
            )
            extension_pins[relative] = pin
            if relative in _EXTENSION_EXECUTABLES:
                executable_pins[target] = pin
        _validate_installed_paths(workspace, extension_pins)
        return executable, executable_pins

    def diagnostic_self_test(self, *, process_user_agent):
        """Fixed read-only azd bootstrap checks; never installs, logs in or deploys."""
        _user_agent(process_user_agent)
        workspace = self.config.workspace_root / uuid4().hex
        checks = {name: False for name in (
            "executable", "version", "extension_registration", "environment_binding",
            "external_credential_requested", "managed_identity_credential", "azd_credential",
            "external_role_management",
        )}
        error_code = None
        deadline = time.monotonic() + MAX_SECONDS
        observation = None
        try:
            executable, pins = self._prepare_workspace(workspace, private_preflight=False)
            registration = workspace / "azd-config" / "config.json"
            if not registration.is_file():
                raise PublicationFailure("azd_extension_missing")
            config = json.loads(registration.read_text(encoding="utf-8"))
            if any(key == "auth" or key.startswith("auth.") for key in config):
                raise PublicationFailure("azd_auth_configuration_invalid")
            installed = config.get("extension", {}).get("installed", {})
            if (set(installed) != _EXTENSION_IDS or any(
                entry.get("id") != identifier or not isinstance(entry.get("version"), str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-beta\.[0-9]+", entry["version"])
                for identifier, entry in installed.items()
            )):
                raise PublicationFailure("azd_extension_missing")
            checks["extension_registration"] = True
            referenced = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", (workspace / "azure.yaml").read_text())
            if not set(referenced).issubset(self.config.values()):
                raise PublicationFailure("azd_environment_binding_invalid")
            env = self._process_env(workspace, process_user_agent)
            checks["external_role_management"] = env["AZD_AGENT_SKIP_ROLE_ASSIGNMENTS"] == "true"
            with self.auth_context(workspace, self.credential, self.config.tenant_id, _AUTH_SCOPES) as observation:
                env.update(observation)
                for name, arguments, fact, failure in (
                    ("version", ["version", "--output", "json", "--no-prompt"],
                     "version_valid", "azd_version_unconfirmed"),
                    ("environment_binding",
                     ["env", "get-values", "--environment", self.config.environment,
                      "--output", "json", "--no-prompt"],
                     "environment_bound", "azd_environment_binding_invalid"),
                    ("azd_credential",
                     ["auth", "token", "--scope", AUTH_PROBE_SCOPE, "--tenant-id", self.config.tenant_id,
                      "--output", "json", "--no-prompt"],
                     "token_credential_valid", "azd_external_credential_roundtrip_unconfirmed"),
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PublicationFailure("deployment_timeout")
                    if name == "azd_credential":
                        clear = getattr(observation, "clear_issued", None)
                        if clear is not None:
                            clear()
                    result = self._run(
                        workspace, executable, pins, env, arguments,
                        failure="azd_diagnostic_command_failed", timeout=min(30, remaining),
                        credential_observation=observation,
                    )
                    if name == "version":
                        checks["executable"] = True
                    checks[name] = getattr(result, fact)
                    if not checks[name]:
                        raise PublicationFailure(failure)
                if not getattr(observation, "credential_observed", False):
                    checks["azd_credential"] = False
                    raise PublicationFailure("azd_external_credential_not_observed")
        except PublicationFailure as error:
            error_code = error.code
        except Exception:
            error_code = "azd_diagnostic_dependency_failed"
        finally:
            if observation is not None:
                checks["external_credential_requested"] = bool(getattr(observation, "credential_requested", False))
                checks["managed_identity_credential"] = bool(getattr(observation, "credential_observed", False))
            if workspace.exists():
                try:
                    shutil.rmtree(workspace)
                except OSError:
                    error_code = "workspace_cleanup_failed"
        return {
            "status": "passed" if error_code is None else "failed", "checks": checks, "error_code": error_code,
            "limitation_code": "azd_external_principal_metadata_unsupported",
        }

    def _publish(self, services, *, process_user_agent):
        _user_agent(process_user_agent)
        config = self.config
        job = PublicationJob(uuid4().hex)
        workspace = config.workspace_root / job.job_id
        try:
            if self.job_store:
                self.job_store(job.as_dict())
            executable, executable_pins = self._prepare_workspace(workspace, private_preflight=True)
            env = self._process_env(workspace, process_user_agent)
            with self.auth_context(workspace, self.credential, config.tenant_id, _AUTH_SCOPES) as auth_env:
                env.update(auth_env)
                for service in services:
                    self._run(
                        workspace, executable, executable_pins, env,
                        ["deploy", service, "--no-prompt",
                         "--environment", config.environment, "--timeout", str(MAX_SECONDS)],
                        failure="deployment_nonzero_exit",
                        credential_observation=auth_env,
                    )
                    job.services[service] = self._outputs(workspace, service)
            job.status = "succeeded"
        except PublicationFailure as error:
            job.status, job.error_code = "failed", error.code
        except Exception:
            job.status, job.error_code = "failed", "publication_dependency_failed"
        finally:
            if workspace.exists():
                try:
                    shutil.rmtree(workspace)
                except OSError:
                    job.status, job.error_code = "failed", "workspace_cleanup_failed"
        if self.job_store:
            # A store failure is not swallowed or reported as a persisted success.
            try:
                self.job_store(job.as_dict())
            except Exception:
                raise PublicationFailure("job_metadata_persistence_failed") from None
        return job


def production_executor(config: OperatorConfig, *, job_store: Callable) -> PublicationExecutor:
    from azure.ai.projects import AIProjectClient
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobClient
    import requests

    if os.name != "posix" or not os.environ.get("IDENTITY_ENDPOINT") or not os.environ.get("IDENTITY_HEADER"):
        raise PublicationFailure("gateway_system_identity_required")
    if not callable(job_store):
        raise ValueError("A durable operator-provided publication job store is required")
    # Explicit empty selectors prevent inherited user-assigned identity configuration.
    credential = ManagedIdentityCredential(
        client_id=None, identity_config={}, retry_total=0,
        connection_timeout=10, read_timeout=30,
    )

    def get_project(cfg, cred):
        token = cred.get_token("https://management.azure.com/.default")
        url = "https://management.azure.com" + cfg.project_id
        headers = {"Authorization": "Bearer " + token.token}
        response = requests.get(
            url, params={"api-version": "2025-06-01"}, headers=headers,
            timeout=30, allow_redirects=False,
        )
        response.raise_for_status()
        return {"id": response.json()["id"]}

    def list_agents(cfg, cred):
        with AIProjectClient(
            cfg.project_endpoint, cred, retry_total=0,
            connection_timeout=10, read_timeout=30,
        ) as client:
            # Force a real HTTPS request, including when no agents exist yet.
            next(iter(client.agents.list()), None)

    def get_agent(cfg, cred, name):
        from azure.core.exceptions import ResourceNotFoundError
        with AIProjectClient(
            cfg.project_endpoint, cred, retry_total=0,
            connection_timeout=10, read_timeout=30,
        ) as client:
            try:
                return client.agents.get(name)
            except ResourceNotFoundError:
                return None

    def blob_factory(endpoint, container, name, cred):
        return BlobClient(
            endpoint, container, name, credential=cred, retry_total=0,
            connection_timeout=10, read_timeout=30,
        )

    return PublicationExecutor(
        config, credential=credential, get_project=get_project,
        list_agents=list_agents, blob_factory=blob_factory, job_store=job_store,
        get_agent=get_agent,
    )
