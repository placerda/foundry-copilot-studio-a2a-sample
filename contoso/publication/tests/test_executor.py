from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from contoso.publication import CommandResult, Document, ExtensionSetup, OperatorConfig, PublicationExecutor, private_dns
from contoso.publication.auth import token_response
from contoso.publication.executor import bounded_command, PublicationFailure


def sha(content):
    return hashlib.sha256(content).hexdigest()


def resolver(host, port, **kwargs):
    return [(None, None, None, None, ("10.20.0.4", port))]


def test_production_manifest_placeholders_are_bound(config):
    import re
    manifest = Path(__file__).parents[2] / "azure.yaml"
    referenced = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", manifest.read_text()))
    assert referenced.issubset(config.values())
    assert config.values()["CONTOSO_ROUTER_SCOPE"] == "catalog"


def test_harness_only_publication_does_not_require_a_deferred_standard_schema(config):
    values = dict(config.environment_values)
    values.update(CONTOSO_ROUTER_SCOPE="studio-harness", CONTOSO_STUDIO_STANDARD_SCHEMA="")
    scoped = replace(config, environment_values=tuple(values.items()))
    assert scoped.values()["CONTOSO_ROUTER_SCOPE"] == "studio-harness"
    assert scoped.values()["CONTOSO_STUDIO_STANDARD_SCHEMA"] == ""
    for unsupported in ("", "invented-scope", "STUDIO-HARNESS"):
        with pytest.raises(ValueError, match="Router scope"):
            replace(config, environment_values=tuple({
                **values, "CONTOSO_ROUTER_SCOPE": unsupported,
            }.items()))
    with pytest.raises(ValueError):
        replace(config, environment_values=tuple({
            **values, "CONTOSO_ROUTER_SCOPE": "catalog",
        }.items()))


@pytest.mark.parametrize("scope", ["catalog", "studio-harness"])
@pytest.mark.parametrize("gateway_url", [
    "https://approved.azurewebsites.net", "https://approved.azure-api.net",
])
def test_publication_admits_apim_tokens_only_for_harness_scope(config, scope, gateway_url):
    values = dict(config.environment_values)
    values.update(CONTOSO_ROUTER_SCOPE=scope, CONTOSO_GATEWAY_URL=gateway_url)
    if scope == "catalog" and gateway_url.endswith(".azure-api.net"):
        with pytest.raises(ValueError):
            replace(config, environment_values=tuple(values.items()))
    else:
        configured = replace(config, environment_values=tuple(values.items()))
        assert configured.values()["CONTOSO_GATEWAY_URL"] == gateway_url


@pytest.mark.parametrize("gateway_url", [
    "http://approved.azure-api.net",
    "https://approved.azure-api.net.evil.example",
    "https://evil.example",
    "https://approved.azure-api.net/api/connections/token",
    "https://user:invalid@approved.azure-api.net",
    "https://approved.azure-api.net?unexpected=value",
    "https://approved.azure-api.net#fragment",
    "https://approved.azure-api.net:443",
    "https://approved.azure-api.net:8443",
])
def test_publication_rejects_unsafe_harness_token_origins(config, gateway_url):
    values = dict(config.environment_values)
    values.update(CONTOSO_ROUTER_SCOPE="studio-harness", CONTOSO_GATEWAY_URL=gateway_url)
    with pytest.raises(ValueError):
        replace(config, environment_values=tuple(values.items()))


@pytest.mark.parametrize("scope", ["catalog", "studio-harness"])
@pytest.mark.parametrize("port", ["", ":443"])
@pytest.mark.parametrize("query", ["api-version=v1", "api-version=2026-03-01-preview"])
def test_business_publication_preserves_native_knowledge_version_query(config, scope, port, query):
    endpoint = (
        f"https://sample.services.ai.azure.com{port}/api/projects/project/"
        f"agents/contoso-knowledge-agent/endpoint/protocols/a2a?{query}"
    )
    values = dict(config.environment_values)
    values.update(CONTOSO_ROUTER_SCOPE=scope, CONTOSO_KNOWLEDGE_A2A_ENDPOINT=endpoint)
    configured = replace(config, environment_values=tuple(values.items()))
    assert configured.values()["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] == endpoint


@pytest.mark.parametrize("suffix", [
    "?access_token=secret",
    "?api-version=v1&access_token=secret",
    "?api-version=v1&api-version=v1",
    "?api-version=%76%31",
    "?api-version=v1#fragment",
    "?api-version=unsupported",
    "/../a2a?api-version=v1",
    "/%2e%2e/a2a?api-version=v1",
])
def test_business_publication_rejects_unsafe_native_knowledge_queries(config, suffix):
    values = dict(config.environment_values)
    values["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] = (
        config.project_endpoint + "/agents/contoso-knowledge-agent/endpoint/protocols/a2a" + suffix
    )
    with pytest.raises(ValueError):
        replace(config, environment_values=tuple(values.items()))


@pytest.mark.parametrize("port", [":80", ":444"])
def test_business_publication_rejects_non_tls_knowledge_ports(config, port):
    values = dict(config.environment_values)
    values["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] = (
        f"https://sample.services.ai.azure.com{port}/api/projects/project/"
        "agents/contoso-knowledge-agent/endpoint/protocols/a2a?api-version=v1"
    )
    with pytest.raises(ValueError):
        replace(config, environment_values=tuple(values.items()))


@pytest.fixture
def config():
    root = Path(__file__).parent / "work" / uuid4().hex
    root.mkdir(parents=True)
    package = root / "package"
    package.mkdir()
    image = "approved.azurecr.io/contoso@sha256:" + "a" * 64
    names = ("contoso-foundry-sdk-agent", "contoso-foundry-agent", "contoso-knowledge-agent")
    manifest = {
        "name": "contoso",
        "services": {
            name: {
                "host": "azure.ai.agent", "kind": "hosted", "language": "docker",
                "image": image, "docker": {"imagePassthrough": True, "remoteBuild": False},
                "protocols": [{"protocol": "responses", "version": "1.0"}],
            }
            for name in names
        },
    }
    data = yaml.safe_dump(manifest).encode()
    (package / "azure.yaml").write_bytes(data)
    (package / "azd").write_bytes(b"fixture-binary")
    (package / "azd-config" / "extensions").mkdir(parents=True)
    (package / "azd-config" / "extensions" / "manifest.json").write_bytes(b"{}")
    documents = []
    for name in ("knowledge-returnables.md", "supplier-delivery-terms.md"):
        content = ("Approved fixture " + name).encode()
        (package / name).write_bytes(content)
        documents.append(Document(name, name, sha(content)))
    subscription = "00000000-0000-0000-0000-000000000001"
    prefix = f"/subscriptions/{subscription}/resourceGroups/approved/providers/"
    result = OperatorConfig(
        package_root=package, workspace_root=root / "jobs", azure_yaml_sha256=sha(data),
        azd_relative_path="azd", azd_sha256=sha(b"fixture-binary"),
        extension_files=(("azd-config/extensions/manifest.json", sha(b"{}")),),
        services=names, image=image, tenant_id="00000000-0000-0000-0000-000000000002",
        subscription_id=subscription, resource_group="approved",
        project_id=prefix + "Microsoft.CognitiveServices/accounts/sample/projects/project",
        project_endpoint="https://sample.services.ai.azure.com/api/projects/project",
        registry_endpoint="https://approved.azurecr.io",
        registry_id=prefix + "Microsoft.ContainerRegistry/registries/approved",
        blob_endpoint="https://approved.blob.core.windows.net", environment="contoso",
        source_storage_id=prefix + "Microsoft.Storage/storageAccounts/approved",
        documents=tuple(documents),
        environment_values=tuple({
            "CONTOSO_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000002",
            "CONTOSO_ENTRA_API_CLIENT_ID": "00000000-0000-0000-0000-000000000003",
            "CONTOSO_STUDIO_ENVIRONMENT_ID": "00000000-0000-0000-0000-000000000004",
            "CONTOSO_STUDIO_STANDARD_SCHEMA": "approved_schema",
            "CONTOSO_STUDIO_HARNESS_ENDPOINT": "https://api.powerplatform.com/copilotstudio-a2a/tenants/00000000-0000-0000-0000-000000000002/environments/00000000-0000-0000-0000-000000000004/agents/cr953_contosociacopilotharness_b8kE5c",
            "CONTOSO_KNOWLEDGE_A2A_ENDPOINT": "https://sample.services.ai.azure.com/api/projects/project/agents/contoso-knowledge-agent/protocols/a2a",
            "CONTOSO_GATEWAY_URL": "https://approved.azurewebsites.net",
            "CONTOSO_SOURCE_BLOB_ENDPOINT": "https://approved.blob.core.windows.net",
            "CONTOSO_KNOWLEDGE_SOURCE_SHA256": documents[0].sha256,
            "CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS": '["opaque-knowledge-key"]',
            "CONTOSO_ROUTER_PLATFORM_CALLER_IDS": '["opaque-router-key"]',
            "CONTOSO_KNOWLEDGE_RUNTIME_ROLE": "knowledge",
            "CONTOSO_ROUTER_RUNTIME_ROLE": "router",
        }.items()),
    )
    yield result
    shutil.rmtree(root)


@contextmanager
def auth(workspace, credential, tenant, scopes):
    yield {"AZD_AUTH_ENDPOINT": "unix:" + str(workspace / "identity" / "auth.sock"),
           "AZD_AUTH_KEY": "in-memory-fixture-key"}


def executor(config, command=None, uploads=None, **overrides):
    uploads = uploads if uploads is not None else []

    class Blob:
        def upload_blob(self, content, **kwargs):
            uploads.append((content, kwargs))
            return {"etag": '"new-etag"'}

    def factory(endpoint, container, name, cred):
        assert endpoint == config.blob_endpoint and container == "sources"
        assert name in {"knowledge-returnables.md", "supplier-delivery-terms.md"}
        assert cred == "fixed-mi"
        return Blob()

    return PublicationExecutor(
        config, credential="fixed-mi",
        get_project=overrides.pop("get_project", lambda cfg, cred: {
            "id": cfg.project_id, "publicNetworkAccess": "Disabled",
            "registryPublicNetworkAccess": "Disabled", "sourcePublicNetworkAccess": "Disabled",
        }),
        list_agents=lambda cfg, cred: None, blob_factory=factory,
        command=command or (lambda *args, **kwargs: 1),
        auth_context=auth, resolver=overrides.pop("resolver", resolver),
        **overrides,
    )


@pytest.mark.parametrize("inherited", [None, os.pathsep.join(("system-bin", "other-bin"))])
def test_nested_sdk_credentials_find_only_the_workspace_azd_first(config, monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", inherited)
    workspace = config.workspace_root / "path-check"
    workspace.mkdir(parents=True)
    env = executor(config)._process_env(workspace, "inline-test")
    expected = [str(workspace / "bin")]
    if inherited:
        expected.extend(inherited.split(os.pathsep))
    assert env["PATH"].split(os.pathsep) == expected
    assert os.environ.get("PATH") == inherited
    assert env["AZD_CONFIG_DIR"] == str(workspace / "azd-config")


def successful_command(config, calls):
    def run(argv, *, cwd, env, timeout):
        calls.append((argv, env.copy(), timeout))
        assert timeout == 210
        assert env["AZD_AUTH_KEY"] == "in-memory-fixture-key"
        assert "AZURE_CLIENT_SECRET" not in env and "AZURE_CLIENT_ID" not in env
        assert env["CI"] == "true"
        service = argv[2]
        prefix = "AGENT_" + service.upper().replace("-", "_") + "_"
        path = cwd / ".azure" / config.environment / ".env"
        with path.open("a", encoding="utf-8") as output:
            output.write(f'{prefix}NAME="{service}"\n{prefix}VERSION="4"\n')
            output.write(f'{prefix}RESPONSES_ENDPOINT="{config.project_endpoint}/agents/{service}/versions/4"\n')
        return 0
    return run


@pytest.mark.parametrize("query,expected", [
    ("api-version=v1", "succeeded"),
    ("api-version=2026-03-15-preview", "succeeded"),
    ("api-version=v1&sig=SECRET", "failed"),
    ("access_token=SECRET", "failed"),
])
@pytest.mark.parametrize("version", ["1", "v1", "1.0.0"])
def test_real_cli_protocol_outputs_exclude_project_metadata_and_reject_secret_queries(config, query, expected, version):
    base = successful_command(config, [])

    def command(argv, *, cwd, env, timeout):
        result = base(argv, cwd=cwd, env=env, timeout=timeout)
        service = argv[2]
        prefix = "AGENT_" + service.upper().replace("-", "_") + "_"
        with (cwd / ".azure" / config.environment / ".env").open("a", encoding="utf-8") as output:
            output.write(f'{prefix}VERSION="{version}"\n')
            output.write(f'{prefix}PROJECT_ENDPOINT="{config.project_endpoint}"\n')
            output.write(
                f'{prefix}RESPONSES_ENDPOINT="{config.project_endpoint}/agents/{service}'
                f'/endpoint/protocols/openai/responses?{query}"\n'
            )
        return result

    job = executor(config, command=command).publish_service(
        "contoso-knowledge-agent", process_user_agent="inline-test",
    )
    assert job.status == expected
    assert "SECRET" not in json.dumps(job.as_dict())
    if expected == "succeeded":
        assert job.services["contoso-knowledge-agent"]["version"] == version
        assert set(job.services["contoso-knowledge-agent"]["endpoints"]) == {"RESPONSES"}
    else:
        assert job.error_code == "deployment_endpoint_mismatch"


@pytest.mark.parametrize("protocols", [
    ("RESPONSES",),
    ("RESPONSES", "A2A"),
    ("RESPONSES", "A2A", "INVOCATIONS"),
])
def test_cli_cleared_protocols_do_not_invalidate_active_endpoints(config, protocols):
    base = successful_command(config, [])
    expected = {}

    def command(argv, *, cwd, env, timeout):
        result = base(argv, cwd=cwd, env=env, timeout=timeout)
        service = argv[2]
        prefix = "AGENT_" + service.upper().replace("-", "_") + "_"
        with (cwd / ".azure" / config.environment / ".env").open("a", encoding="utf-8") as output:
            for protocol in ("RESPONSES", "A2A", "INVOCATIONS", "INVOCATIONS_WS"):
                endpoint = ""
                if protocol in protocols:
                    suffix = "openai/responses" if protocol == "RESPONSES" else protocol.lower()
                    endpoint = (
                        f"{config.project_endpoint}/agents/{service}"
                        f"/endpoint/protocols/{suffix}?api-version=v1"
                    )
                    expected[protocol] = endpoint
                output.write(f'{prefix}{protocol}_ENDPOINT="{endpoint}"\n')
        return result

    job = executor(config, command=command).publish_service(
        "contoso-foundry-agent", process_user_agent="inline-test",
    )
    assert job.status == "succeeded"
    assert job.services["contoso-foundry-agent"]["endpoints"] == expected


@pytest.mark.parametrize("value", ["", None, " ", "null", "https://evil.example/SECRET"])
def test_cli_cleared_endpoints_do_not_mask_missing_or_invalid_outputs(config, value):
    base = successful_command(config, [])

    def command(argv, *, cwd, env, timeout):
        result = base(argv, cwd=cwd, env=env, timeout=timeout)
        prefix = "AGENT_" + argv[2].upper().replace("-", "_") + "_"
        with (cwd / ".azure" / config.environment / ".env").open("a", encoding="utf-8") as output:
            for protocol in ("RESPONSES", "A2A", "INVOCATIONS", "INVOCATIONS_WS"):
                line = prefix + protocol + "_ENDPOINT"
                if protocol != "RESPONSES" or value == "":
                    output.write(f'{line}=""\n')
                elif value is None:
                    output.write(line + "\n")
                else:
                    output.write(f'{line}="{value}"\n')
        return result

    job = executor(config, command=command).publish_service(
        "contoso-foundry-agent", process_user_agent="inline-test",
    )
    assert job.status == "failed"
    assert job.error_code == (
        "deployment_outputs_missing" if value == "" else "deployment_endpoint_mismatch"
    )
    assert job.services == {}
    assert "SECRET" not in json.dumps(job.as_dict())


@pytest.mark.parametrize("change", [
    {"services": ("one", "two", "two")},
    {"services": ("one", "two")},
    {"services": ("one", "two", "--all")},
    {"image": "evil.example/image:latest"},
    {"image": "approved.azurecr.io/contoso:latest"},
    {"project_endpoint": "https://evil.example/api/projects/project"},
    {"blob_endpoint": "https://approved.blob.core.windows.net/?sig=secret"},
    {"azd_relative_path": "../other.exe"},
    {"environment": "contoso\nAZURE_CLIENT_SECRET=bad"},
    {"environment_values": (("AZURE_CLIENT_SECRET", "secret"),)},
    {"extension_files": (("azd-config/auth.json", "a" * 64),)},
    {"project_id": "/subscriptions/other"},
    {"documents": (Document("knowledge-returnables.md", "a", "a" * 64),) * 2},
])
def test_rejects_operator_config(config, change):
    with pytest.raises((ValueError, TypeError)):
        replace(config, **change)


def test_exact_commands_and_outputs(config, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "administrator-secret")
    monkeypatch.setenv("AZURE_CLIENT_ID", "administrator-id")
    calls, uploads = [], []
    worker = executor(config, command=successful_command(config, calls), uploads=uploads)
    sources = worker.publish_sources()
    job = worker.publish(process_user_agent="inline-test-agent")
    assert job.status == "succeeded"
    assert len(calls) == 3 and len(uploads) == 2
    assert job.sources == {} and len(sources) == 2
    for service, (argv, env, _) in zip(config.services, calls):
        assert argv == [
            str(config.workspace_root / job.job_id / "bin" / "azd"), "deploy", service, "--no-prompt",
            "--environment", "contoso", "--timeout", "210",
        ]
        assert env["AZURE_DEV_USER_AGENT"] == "inline-test-agent"
        assert job.services[service]["version"] == "4"
    assert not (config.workspace_root / job.job_id).exists()
    assert "administrator" not in json.dumps(job.as_dict())
    assert not any("inline-test-agent" in p.read_text(errors="ignore")
                   for p in config.package_root.rglob("*") if p.is_file())


def test_nonzero_is_failure_and_no_streams(config):
    calls = []
    def failure(argv, **kwargs):
        calls.append(argv)
        return 31
    job = executor(config, command=failure).publish(process_user_agent="inline-test")
    assert job.status == "failed" and job.error_code == "deployment_nonzero_exit"
    assert len(calls) == 1 and job.services == {}


def test_zero_without_env_outputs_is_not_success(config):
    job = executor(config, command=lambda *a, **k: 0).publish(process_user_agent="inline-test")
    assert job.error_code == "deployment_outputs_missing"
    assert job.status == "failed"


def test_environment_endpoint_mismatch(config):
    calls = []
    success = successful_command(config, calls)
    def wrong(argv, **kwargs):
        success(argv, **kwargs)
        path = kwargs["cwd"] / ".azure" / "contoso" / ".env"
        path.write_text(path.read_text().replace("https://sample.services", "https://other.services"))
        return 0
    job = executor(config, command=wrong).publish(process_user_agent="inline-test")
    assert job.error_code == "deployment_environment_mismatch"


def test_output_endpoint_cannot_escape_project(config):
    calls = []
    success = successful_command(config, calls)
    def wrong(argv, **kwargs):
        success(argv, **kwargs)
        path = kwargs["cwd"] / ".azure" / "contoso" / ".env"
        text = path.read_text().replace("/agents/" + argv[2], "/../other/agents/" + argv[2])
        path.write_text(text)
        return 0
    # URL-path normalization must never turn an output into a different project.
    job = executor(config, command=wrong).publish(process_user_agent="inline-test")
    assert job.status == "failed"


def test_source_pins_prevent_all_writes(config):
    uploads = []
    (config.package_root / config.documents[1].name).write_bytes(b"modified")
    with pytest.raises(PublicationFailure, match="source_hash_mismatch"):
        executor(config, uploads=uploads).publish_sources()
    assert uploads == []


def test_source_etag_condition(config):
    uploads = []
    documents = tuple(replace(d, expected_etag='"old-etag"') for d in config.documents)
    configured = replace(config, documents=documents)
    executor(configured, uploads=uploads).publish_sources()
    from azure.core import MatchConditions
    assert all(k["overwrite"] and k["etag"] == '"old-etag"'
               and k["match_condition"] == MatchConditions.IfNotModified
               for _, k in uploads)


@pytest.mark.parametrize("answers", [[], ["8.8.8.8"], ["127.0.0.1"], ["169.254.169.254"],
                                   ["::1"], ["10.0.0.2", "20.0.0.1"]])
def test_private_dns_rejects_every_non_private_answer(answers):
    with pytest.raises(PublicationFailure):
        private_dns("approved", lambda *a, **k: [
            (None, None, None, None, (address, 443)) for address in answers
        ])


def test_private_preflight_stops_commands(config):
    job = executor(config, resolver=lambda *a, **k: [
        (None, None, None, None, ("20.0.0.2", 443)),
    ]).publish(process_user_agent="inline-test")
    assert job.error_code == "private_dns_required" and job.sources == {}


@pytest.mark.parametrize("kind", ["manifest", "binary", "extension"])
def test_package_pins(config, kind):
    target = {"manifest": "azure.yaml", "binary": "azd",
              "extension": "azd-config/extensions/manifest.json"}[kind]
    (config.package_root / target).write_bytes(b"tampered")
    job = executor(config).publish(process_user_agent="inline-test")
    assert job.error_code in {"package_hash_mismatch", "binary_hash_mismatch", "extension_hash_mismatch"}


def test_even_hash_approved_hooks_are_denied(config):
    path = config.package_root / "azure.yaml"
    data = yaml.safe_load(path.read_text())
    data["hooks"] = {"predeploy": {"run": "arbitrary"}}
    path.write_text(yaml.safe_dump(data))
    job = executor(replace(config, azure_yaml_sha256=sha(path.read_bytes()))).publish(
        process_user_agent="inline-test",
    )
    assert job.error_code == "unapproved_manifest"


def test_production_streams_bounded_and_only_fixed_codes_escape(monkeypatch, config):
    def run(argv, **kwargs):
        assert kwargs["shell"] is False and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        return SimpleNamespace(
            stdout=io.BytesIO(b"accessToken=SECRET"), stderr=io.BytesIO(b"unknown flag: --invalid SECRET"),
            wait=lambda **kwargs: 7, poll=lambda: 7,
        )
    monkeypatch.setattr(subprocess, "Popen", run)
    result = bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=210)
    assert result == CommandResult(7, "azd_invalid_flags")
    assert "SECRET" not in repr(result)


def test_timeout_safe(monkeypatch, config):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("SECRET", 210, output="bearer SECRET", stderr="SECRET")
    monkeypatch.setattr(subprocess, "Popen", run)
    with pytest.raises(PublicationFailure, match="deployment_timeout") as caught:
        bounded_command(["fixed"], cwd=config.package_root, env={}, timeout=210)
    assert "SECRET" not in str(caught.value)


def test_bridge_requests_are_scope_and_tenant_allowlisted():
    calls = []
    cred = SimpleNamespace(get_token=lambda *scopes: (
        calls.append(scopes) or SimpleNamespace(token="in-memory", expires_on=2000000000)
    ))
    response = token_response(cred, {"scopes": ["https://ai.azure.com/.default"]}, "tenant",
                              frozenset({"https://ai.azure.com/.default"}))
    assert response["status"] == "success" and response["expiresOn"].endswith("+00:00")
    for request in ({"scopes": ["https://evil/.default"]},
                    {"scopes": ["https://ai.azure.com/.default"], "tenantId": "other"},
                    {"scopes": ["https://ai.azure.com/.default"], "claims": "bad"}):
        with pytest.raises(ValueError):
            token_response(cred, request, "tenant", frozenset({"https://ai.azure.com/.default"}))
    assert len(calls) == 1


def test_go_arm_audience_is_explicitly_allowed_without_general_scope_normalization():
    from contoso.publication.auth import AUTH_PROBE_SCOPE
    from contoso.publication.executor import _AUTH_SCOPES

    calls = []
    credential = SimpleNamespace(get_token=lambda *scopes: (
        calls.append(scopes) or SimpleNamespace(token="in-memory", expires_on=2000000000)
    ))
    assert AUTH_PROBE_SCOPE == "https://management.core.windows.net//.default"
    token_response(credential, {"scopes": [AUTH_PROBE_SCOPE]}, "tenant", _AUTH_SCOPES)
    assert calls == [(AUTH_PROBE_SCOPE,)]
    for scope in (
        "https://graph.microsoft.com/.default",
        "https://management.core.windows.net.evil.example//.default",
        "https://management.core.usgovcloudapi.net//.default",
        "https://management.core.windows.net///.default",
    ):
        with pytest.raises(ValueError):
            token_response(credential, {"scopes": [scope]}, "tenant", _AUTH_SCOPES)
    assert len(calls) == 1


def test_foundry_deploy_endpoint_is_derived_from_the_approved_project(config):
    values = config.values()
    assert values["FOUNDRY_PROJECT_ENDPOINT"] == config.project_endpoint
    assert values["FOUNDRY_PROJECT_ENDPOINT"] == values["AZURE_AI_PROJECT_ENDPOINT"]


def test_durable_job_store_records_real_terminal_status(config):
    records, calls = [], []
    worker = executor(config, command=successful_command(config, calls),
                      job_store=lambda job: records.append(json.loads(json.dumps(job))))
    job = worker.publish(process_user_agent="inline-test")
    assert records[0]["status"] == "running"
    assert records[-1] == job.as_dict()
    assert records[-1]["status"] == "succeeded"


def test_store_failure_cannot_be_reported_as_persisted_success(config):
    def unavailable(job):
        raise OSError("storage secret unavailable")
    worker = executor(config, job_store=unavailable)
    with pytest.raises(PublicationFailure, match="job_metadata_persistence_failed"):
        worker.publish(process_user_agent="inline-test")


def test_literal_image_placeholder_is_operator_bound(config):
    path = config.package_root / "azure.yaml"
    path.write_text(path.read_text().replace(config.image, "${CONTOSO_RUNTIME_IMAGE}"))
    configured = replace(config, azure_yaml_sha256=sha(path.read_bytes()))
    calls = []
    job = executor(configured, command=successful_command(configured, calls)).publish(
        process_user_agent="inline-test",
    )
    assert job.status == "succeeded"
    assert all(env["CONTOSO_RUNTIME_IMAGE"] == config.image for _, env, _ in calls)


def test_production_factory_requires_gateway_identity_and_never_cli(config, monkeypatch):
    from contoso.publication import production_executor
    import os
    if os.name == "posix":
        monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
        monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    with pytest.raises(PublicationFailure, match="gateway_system_identity_required"):
        production_executor(config, job_store=lambda metadata: None)


def configure_env(config, **changes):
    values = dict(config.environment_values)
    values.update(changes)
    return replace(config, environment_values=tuple(values.items()))


@pytest.mark.parametrize("schema", ["new_CONTOSOSTUDIOStandard", "cr953_CONTOSOSTUDIOStandard", "cr953_agent.schema-v2"])
def test_standard_schema_is_an_identifier_not_an_endpoint(config, schema, monkeypatch):
    from contoso.publication import executor as module
    original = module._runtime_url

    def validate_url(value, *args, **kwargs):
        assert value != schema
        return original(value, *args, **kwargs)

    monkeypatch.setattr(module, "_runtime_url", validate_url)
    configured = configure_env(config, CONTOSO_STUDIO_STANDARD_SCHEMA=schema)
    calls = []
    job = executor(configured, command=successful_command(configured, calls)).publish_service(
        "contoso-foundry-sdk-agent", process_user_agent="inline-test",
    )
    assert job.status == "succeeded"
    assert calls[0][1]["CONTOSO_STUDIO_STANDARD_SCHEMA"] == schema


@pytest.mark.parametrize("schema", [
    "https://approved.environment.api.powerplatform.com/agent",
    "cr953/agent", r"cr953\agent", "../agent", "agent?query=1",
    "new_CONTOSOSTUDIOStandard\n", "agent\r", "agent\x00", "agent\tname",
    " agent", "agent ", "é_agent", "a" * 129,
])
def test_standard_schema_rejects_urls_paths_and_controls(config, schema):
    with pytest.raises(ValueError, match="Invalid explicit STUDIO schema"):
        configure_env(config, CONTOSO_STUDIO_STANDARD_SCHEMA=schema)


def test_complete_real_binding_shape_accepts_native_harness_and_probe_empty_values(config):
    configured = configure_env(
        config, CONTOSO_STUDIO_STANDARD_SCHEMA="new_CONTOSOSTUDIOStandard",
        CONTOSO_KNOWLEDGE_RUNTIME_ROLE="identity-probe", CONTOSO_ROUTER_RUNTIME_ROLE="identity-probe",
        CONTOSO_KNOWLEDGE_A2A_ENDPOINT="", CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS="[]",
        CONTOSO_ROUTER_PLATFORM_CALLER_IDS="[]",
        CONTOSO_SOURCE_BLOB_ENDPOINT=config.blob_endpoint + "/",
    )
    configured = replace(configured, blob_endpoint=config.blob_endpoint + "/")
    assert configured.identity_probe
    assert configured.values()["CONTOSO_STUDIO_STANDARD_SCHEMA"] == "new_CONTOSOSTUDIOStandard"
    assert configured.values()["CONTOSO_STUDIO_HARNESS_ENDPOINT"].startswith(
        "https://api.powerplatform.com/copilotstudio-a2a/tenants/",
    )
    assert configured.values()["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] == ""


@pytest.mark.parametrize("endpoint", [
    "https://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://api.powerplatform.com:443/copilotstudio-a2a/environments/approved/agents/cr953_agent",
])
def test_harness_endpoint_matches_existing_native_a2a_contract(config, endpoint):
    assert configure_env(config, CONTOSO_STUDIO_HARNESS_ENDPOINT=endpoint).values()["CONTOSO_STUDIO_HARNESS_ENDPOINT"] == endpoint


@pytest.mark.parametrize("endpoint", [
    "https://approved.environment.api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://api.powerplatform.com.evil.example/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://evilapi.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "http://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://api.powerplatform.com:8443/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://api.powerplatform.com/copilotstudio/conversations",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent?api-version=2022-03-01-preview",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent?token=secret",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/../agents/cr953_agent",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/%2e%2e/agents/cr953_agent",
    "https://user:secret@api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent",
    "https://api.powerplatform.com/copilotstudio-a2a/environments/approved/agents/cr953_agent#fragment",
])
def test_harness_rejects_wrong_hosts_standard_discovery_and_unsafe_paths(config, endpoint):
    with pytest.raises(ValueError):
        configure_env(config, CONTOSO_STUDIO_HARNESS_ENDPOINT=endpoint)


@pytest.mark.skipif(
    not os.environ.get("CONTOSO_PUBLICATION_TEST_BINDINGS") or not os.environ.get("CONTOSO_PUBLICATION_TEST_PACKAGE"),
    reason="Requires the operator's explicitly supplied nonsecret binding fixture and staged package",
)
def test_entire_operator_supplied_binding_fixture_with_exact_parent_constructor(config):
    from contoso.gateway_bootstrap import operator_config

    package = Path(os.environ["CONTOSO_PUBLICATION_TEST_PACKAGE"])
    binding_path = Path(os.environ["CONTOSO_PUBLICATION_TEST_BINDINGS"])
    binding_bytes = binding_path.read_bytes()
    manifest_bytes = (package / "manifest.json").read_bytes()
    bindings = json.loads(binding_bytes)
    actual = operator_config({
        "CONTOSO_PUBLICATION_BINDINGS": json.dumps(bindings, separators=(",", ":")),
        "CONTOSO_PUBLICATION_MANIFEST_SHA256": sha(manifest_bytes),
        "CONTOSO_PUBLICATION_WORKSPACE_ROOT": str(config.workspace_root),
    }, package=package)
    assert dict(actual.environment_values) == bindings["environment_values"]
    assert actual.identity_probe == (
        bindings["environment_values"]["CONTOSO_KNOWLEDGE_RUNTIME_ROLE"] == "identity-probe"
        and bindings["environment_values"]["CONTOSO_ROUTER_RUNTIME_ROLE"] == "identity-probe"
    )
    assert actual.services == ("contoso-knowledge-agent", "contoso-foundry-sdk-agent", "contoso-foundry-agent")
    assert actual.image == bindings["image"]
    assert actual.project_endpoint == bindings["project_endpoint"]
    assert actual.source_storage_id == bindings["source_storage_id"]
    assert sha((package / actual.azd_relative_path).read_bytes()) == actual.azd_sha256
    for relative, pin in actual.extension_files:
        assert sha((package / relative).read_bytes()) == pin
    for document in actual.documents:
        assert sha((package / document.relative_path).read_bytes()) == document.sha256
    executor(actual)._manifest()
    assert binding_path.read_bytes() == binding_bytes
    assert (package / "manifest.json").read_bytes() == manifest_bytes


def test_shared_operator_configuration_preserves_pins_and_fixed_targets(config):
    from contoso.publication.runtime import operator_config
    from contoso.gateway_bootstrap import operator_config as gateway_config

    assert gateway_config is operator_config
    package = config.package_root
    (package / "tools").mkdir()
    (package / "tools" / "azd").write_bytes((package / config.azd_relative_path).read_bytes())
    (package / "knowledge").mkdir()
    for document in config.documents:
        (package / "knowledge" / document.name).write_bytes(
            (package / document.relative_path).read_bytes(),
        )
    manifest = {
        "azure_yaml_sha256": config.azure_yaml_sha256, "azd_sha256": config.azd_sha256,
        "extension_files": config.extension_files,
        "documents": {document.name: document.sha256 for document in config.documents},
    }
    manifest_bytes = json.dumps(manifest).encode()
    (package / "manifest.json").write_bytes(manifest_bytes)
    bindings = {key: getattr(config, key) for key in (
        "image", "tenant_id", "subscription_id", "resource_group", "project_id",
        "project_endpoint", "registry_endpoint", "registry_id", "blob_endpoint", "source_storage_id",
    )}
    bindings["environment_values"] = dict(config.environment_values)
    env = {
        "CONTOSO_PUBLICATION_MANIFEST_SHA256": sha(manifest_bytes),
        "CONTOSO_PUBLICATION_BINDINGS": json.dumps(bindings),
        "CONTOSO_PUBLICATION_WORKSPACE_ROOT": str(config.workspace_root),
    }
    actual = operator_config(env, package=package)
    assert actual.project_id == config.project_id
    assert actual.environment_values == config.environment_values
    assert actual.azure_yaml_sha256 == config.azure_yaml_sha256
    assert actual.azd_sha256 == config.azd_sha256
    assert actual.extension_files == config.extension_files
    assert set(actual.services) == set(config.services)
    assert actual.workspace_root == config.workspace_root
    assert actual.environment == "contoso-demo"
    assert actual.azd_relative_path == "tools/azd"
    assert {document.name: document.sha256 for document in actual.documents} == manifest["documents"]
    with pytest.raises(PublicationFailure, match="publication_manifest_changed"):
        operator_config({**env, "CONTOSO_PUBLICATION_MANIFEST_SHA256": "0" * 64}, package=package)
    with pytest.raises(ValueError, match="Duplicate publication field"):
        operator_config({
            **env, "CONTOSO_PUBLICATION_BINDINGS": '{"image":"first","image":"second"}',
        }, package=package)


@pytest.mark.parametrize("key,value", [
    ("CONTOSO_ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000099"),
    ("CONTOSO_ENTRA_API_CLIENT_ID", "not-an-id"),
    ("CONTOSO_STUDIO_ENVIRONMENT_ID", ""),
    ("CONTOSO_STUDIO_STANDARD_SCHEMA", ""),
    ("CONTOSO_STUDIO_HARNESS_ENDPOINT", "https://evil.example/provider"),
    ("CONTOSO_STUDIO_HARNESS_ENDPOINT", "https://approved.environment.api.powerplatform.com/?sig=secret"),
    ("CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "https://other.services.ai.azure.com/api/projects/project/agents/contoso-knowledge-agent/a2a"),
    ("CONTOSO_GATEWAY_URL", "http://approved.azurewebsites.net"),
    ("CONTOSO_GATEWAY_URL", "https://approved.azurewebsites.net/api/arbitrary"),
    ("CONTOSO_SOURCE_BLOB_ENDPOINT", "https://other.blob.core.windows.net"),
    ("CONTOSO_KNOWLEDGE_SOURCE_SHA256", "b" * 64),
    ("CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS", "[]"),
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", ""),
    ("CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS", '["duplicate", "duplicate"]'),
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", '{"arbitrary": "key"}'),
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", '[123]'),
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", '["bad\\nkey"]'),
    ("CONTOSO_KNOWLEDGE_RUNTIME_ROLE", "identity-link-probe"),
    ("CONTOSO_ROUTER_RUNTIME_ROLE", "knowledge"),
])
def test_explicit_runtime_bindings(config, key, value):
    with pytest.raises((ValueError, TypeError)):
        configure_env(config, **{key: value})


def probe_config(config):
    return configure_env(
        config, CONTOSO_KNOWLEDGE_RUNTIME_ROLE="identity-probe",
        CONTOSO_ROUTER_RUNTIME_ROLE="identity-probe", CONTOSO_STUDIO_ENVIRONMENT_ID="",
        CONTOSO_STUDIO_STANDARD_SCHEMA="", CONTOSO_STUDIO_HARNESS_ENDPOINT="",
        CONTOSO_KNOWLEDGE_A2A_ENDPOINT="", CONTOSO_GATEWAY_URL="",
        CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS="", CONTOSO_ROUTER_PLATFORM_CALLER_IDS="[]",
    )


def test_bootstrap_roles_require_explicit_pair_and_do_not_upload(config):
    configured = probe_config(config)
    calls, uploads = [], []
    worker = executor(configured, command=successful_command(configured, calls), uploads=uploads)
    job = worker.publish_service("contoso-knowledge-agent", process_user_agent="inline-test")
    assert job.status == "succeeded" and job.sources == {} and uploads == []
    assert configured.identity_probe
    assert len(calls) == 1


def test_all_runtime_bindings_required_even_probe(config):
    values = dict(probe_config(config).environment_values)
    del values["CONTOSO_STUDIO_STANDARD_SCHEMA"]
    with pytest.raises(ValueError):
        replace(config, environment_values=tuple(values.items()))


def link_config(config):
    return configure_env(
        probe_config(config),
        CONTOSO_ROUTER_RUNTIME_ROLE="identity-link-probe",
        CONTOSO_ROUTER_PLATFORM_CALLER_IDS='["opaque-gateway-platform-key"]',
        CONTOSO_KNOWLEDGE_A2A_ENDPOINT=(
            config.project_endpoint + "/agents/contoso-knowledge-agent/versions/4/protocols/a2a"
        ),
    )


def test_link_bootstrap_requires_explicit_router_caller_and_fixed_endpoint(config):
    configured = link_config(config)
    values = dict(configured.environment_values)
    assert values["CONTOSO_KNOWLEDGE_RUNTIME_ROLE"] == "identity-probe"
    assert values["CONTOSO_ROUTER_RUNTIME_ROLE"] == "identity-link-probe"
    assert values["CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS"] == ""
    assert not configured.identity_probe
    calls, uploads = [], []
    job = executor(configured, command=successful_command(configured, calls), uploads=uploads).publish_service(
        "contoso-foundry-sdk-agent", process_user_agent="inline-test",
    )
    assert job.status == "succeeded" and len(calls) == 1 and uploads == []
    assert calls[0][1]["CONTOSO_ROUTER_RUNTIME_ROLE"] == "identity-link-probe"
    assert calls[0][1]["CONTOSO_ROUTER_PLATFORM_CALLER_IDS"] == values["CONTOSO_ROUTER_PLATFORM_CALLER_IDS"]
    assert calls[0][1]["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"] == values["CONTOSO_KNOWLEDGE_A2A_ENDPOINT"]


@pytest.mark.parametrize("key,value", [
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", ""),
    ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", "[]"),
    ("CONTOSO_KNOWLEDGE_A2A_ENDPOINT", ""),
    ("CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "https://other.services.ai.azure.com/api/projects/project/agents/contoso-knowledge-agent/a2a"),
    ("CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "https://sample.services.ai.azure.com/api/projects/project/agents/other/a2a"),
    ("CONTOSO_KNOWLEDGE_RUNTIME_ROLE", "identity-link-probe"),
])
def test_link_bootstrap_rejects_missing_or_unapproved_link_bindings(config, key, value):
    with pytest.raises(ValueError):
        configure_env(link_config(config), **{key: value})


@pytest.mark.parametrize("knowledge_role", ["identity-probe", "knowledge"])
@pytest.mark.parametrize("router_role", ["identity-probe", "identity-link-probe", "router"])
def test_each_role_has_its_own_explicit_caller_requirement(config, knowledge_role, router_role):
    configured = configure_env(
        config, CONTOSO_KNOWLEDGE_RUNTIME_ROLE=knowledge_role, CONTOSO_ROUTER_RUNTIME_ROLE=router_role,
    )
    for key, role in (
        ("CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS", knowledge_role),
        ("CONTOSO_ROUTER_PLATFORM_CALLER_IDS", router_role),
    ):
        for empty in ("", "[]"):
            if role == "identity-probe":
                configure_env(configured, **{key: empty})
            else:
                with pytest.raises(ValueError):
                    configure_env(configured, **{key: empty})


@pytest.mark.parametrize("key", [
    "CONTOSO_STUDIO_ENVIRONMENT_ID", "CONTOSO_STUDIO_STANDARD_SCHEMA", "CONTOSO_STUDIO_HARNESS_ENDPOINT",
    "CONTOSO_KNOWLEDGE_A2A_ENDPOINT", "CONTOSO_GATEWAY_URL",
])
def test_full_router_keeps_all_required_fields_when_knowledge_still_probe(config, key):
    configured = configure_env(
        config, CONTOSO_KNOWLEDGE_RUNTIME_ROLE="identity-probe", CONTOSO_KNOWLEDGE_PLATFORM_CALLER_IDS="[]",
    )
    with pytest.raises(ValueError):
        configure_env(configured, **{key: ""})


def test_link_bootstrap_never_defaults_missing_fields(config):
    configured = link_config(config)
    for key in dict(configured.environment_values):
        values = dict(configured.environment_values)
        del values[key]
        with pytest.raises(ValueError):
            replace(configured, environment_values=tuple(values.items()))


def test_opaque_platform_keys_survive_env_not_coerced_to_oid(config):
    configured = configure_env(
        config, CONTOSO_ROUTER_PLATFORM_CALLER_IDS=json.dumps(['opaque/sdk+key==', 'key"withquote']),
    )
    calls = []
    job = executor(configured, command=successful_command(configured, calls)).publish_service(
        "contoso-foundry-sdk-agent", process_user_agent="inline-test",
    )
    assert job.status == "succeeded"
    assert calls[0][1]["CONTOSO_ROUTER_PLATFORM_CALLER_IDS"] == dict(configured.environment_values)["CONTOSO_ROUTER_PLATFORM_CALLER_IDS"]


def test_publish_one_service_one_deploy_no_source_repeat(config):
    records, calls, uploads = [], [], []
    worker = executor(config, command=successful_command(config, calls), uploads=uploads,
                      job_store=lambda value: records.append(json.loads(json.dumps(value))))
    for service in config.services:
        job = worker.publish_service(service, process_user_agent="inline-test")
        assert job.status == "succeeded" and set(job.services) == {service}
    assert len(calls) == 3 and uploads == []
    assert len(records) == 6


@pytest.mark.parametrize("service", ["arbitrary", "--all", "", None, []])
def test_unknown_service_fails_before_all_side_effects(config, service):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid service caused a side effect")
    worker = executor(config, command=forbidden, get_project=forbidden, job_store=forbidden)
    with pytest.raises(ValueError):
        worker.publish_service(service, process_user_agent="inline-test")
    assert not config.workspace_root.exists()


def test_fixed_sdk_records_after_partial_deploy_without_any_secret(config):
    calls = []
    def get_agent(cfg, cred, name):
        calls.append(name)
        assert cfg is config and cred == "fixed-mi"
        if name == "contoso-foundry-agent":
            return None
        return {
            "name": name,
            "versions": {"latest": {"version": "7", "status": "failed",
                                    "definition": {"environment_variables": {"secret": "NEVER"}}}},
            "instance_identity": {
                "principal_id": "00000000-0000-0000-0000-000000000005",
                "client_id": "00000000-0000-0000-0000-000000000006",
                "status": "active", "token": "NEVER",
            },
            "agent_endpoint": {
                "authorization_schemes": [{"key": "NEVER"}],
                "protocol_configuration": {"responses": {}, "a2a": {"secret": "NEVER"}},
                "version_selector": {"version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "7", "secret": "NEVER"},
                ]},
            },
            "logs": "NEVER",
        }
    records = executor(config, get_agent=get_agent).get_agent_records()
    assert calls == list(config.services)
    assert records["contoso-knowledge-agent"]["status"] == "failed"
    assert records["contoso-foundry-agent"]["status"] == "not_found"
    assert records["contoso-knowledge-agent"]["instance_identity"]["principal_id"].endswith("0005")
    assert "NEVER" not in json.dumps(records)
    assert not config.workspace_root.exists()


def test_sdk_records_do_not_turn_opaque_key_into_entra_principal(config):
    with pytest.raises(PublicationFailure, match="agent_record_lookup_failed"):
        executor(config, get_agent=lambda *args: {
            "name": args[-1], "instance_identity": {"principal_id": "opaque-sdk-key"},
        }).get_agent_records()


def test_sdk_lookup_failure_is_safe_not_empty_success(config):
    def failure(*args):
        raise RuntimeError("Bearer SECRET")
    with pytest.raises(PublicationFailure, match="agent_record_lookup_failed") as error:
        executor(config, get_agent=failure).get_agent_records()
    assert "SECRET" not in str(error.value)


def test_records_with_actual_pinned_sdk_models(config):
    from azure.ai.projects.models import AgentDetails
    def get_agent(cfg, cred, name):
        return AgentDetails({
            "name": name,
            "versions": {"latest": {"version": "9", "status": "creating"}},
            "instance_identity": {
                "principal_id": "00000000-0000-0000-0000-000000000007",
                "client_id": "00000000-0000-0000-0000-000000000008",
                "status": "active",
            },
            "agent_endpoint": {"protocol_configuration": {"responses": {}, "a2a": {}}},
        })
    records = executor(config, get_agent=get_agent).get_agent_records()
    assert all(record["version"] == "9" and record["status"] == "creating"
               for record in records.values())
    assert records["contoso-knowledge-agent"]["agent_endpoint"] == {
        "protocol_configuration": {"responses": {}, "a2a": {}},
    }


@pytest.mark.parametrize("version", ["1", "v1", "1.0.0"])
def test_sdk_version_identifiers_are_not_assumed_to_be_decimal_numbers(config, version):
    from azure.ai.projects.models import AgentDetails

    def get_agent(cfg, cred, name):
        return AgentDetails({
            "name": name,
            "versions": {"latest": {"version": version, "status": "active"}},
            "agent_endpoint": {"version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": version},
            ]}},
        })

    records = executor(config, get_agent=get_agent).get_agent_records()
    assert all(record["version"] == version for record in records.values())
    assert records["contoso-knowledge-agent"]["agent_endpoint"]["version_selector"][
        "version_selection_rules"
    ][0]["agent_version"] == version


@pytest.mark.parametrize("version", ["../1", "v1?secret=bad", "v1#bad", "v1%2Fbad", "v1 bad", "v" * 65])
def test_sdk_version_identifiers_cannot_escape_the_endpoint_contract(config, version):
    with pytest.raises(PublicationFailure, match="agent_record_version_invalid"):
        executor(config, get_agent=lambda *args: {
            "name": args[-1], "versions": {"latest": {"version": version, "status": "active"}},
        }).get_agent_records()


@pytest.mark.parametrize("version", ["../1", "v1?secret=bad", "v1#bad", "v1%2Fbad", "v1 bad", "v" * 129])
def test_selector_version_identifiers_cannot_escape_the_endpoint_contract(config, version):
    with pytest.raises(PublicationFailure, match="agent_record_selector_version_invalid"):
        executor(config, get_agent=lambda *args: {
            "name": args[-1], "versions": {"latest": {"version": "1", "status": "active"}},
            "agent_endpoint": {"version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": version},
            ]}},
        }).get_agent_records()


@pytest.mark.parametrize("version,code", [
    (None, "missing"), (1, "type_invalid"), (True, "type_invalid"),
    ("", "empty"),
])
def test_unbound_selector_reports_only_a_fixed_safe_category(config, version, code):
    with pytest.raises(PublicationFailure, match="agent_record_selector_version_" + code):
        executor(config, get_agent=lambda *args: {
            "name": args[-1], "versions": {"latest": {"version": "1", "status": "active"}},
            "agent_endpoint": {"version_selector": {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": version, "secret": "NEVER"},
            ]}},
        }).get_agent_records()


@pytest.mark.parametrize("versions", [None, {}, {"latest": None}, {"latest": {}}])
def test_created_agent_without_a_version_is_not_a_ready_runtime(config, versions):
    from azure.ai.projects.models import AgentDetails

    def get_agent(cfg, cred, name):
        return AgentDetails({
            "name": name, "versions": versions,
            "agent_endpoint": {
                "version_selector": {"version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": None},
                ]},
                "authorization_schemes": [{"key": "NEVER"}],
            },
        })

    records = executor(config, get_agent=get_agent).get_agent_records()
    assert all(record["status"] == "not_deployed" and record["version"] is None
               and record["agent_endpoint"] is None for record in records.values())
    assert "NEVER" not in json.dumps(records)


def test_active_status_without_an_observed_version_cannot_pass_readiness(config):
    with pytest.raises(PublicationFailure, match="agent_record_active_version_missing"):
        executor(config, get_agent=lambda *args: {
            "name": args[-1], "versions": {"latest": {"status": "active"}},
        }).get_agent_records()


def test_metadata_only_read_cannot_hide_a_failed_normal_endpoint_validation(config):
    def get_agent(cfg, cred, name):
        return {
            "name": name, "versions": {"latest": {"version": "1", "status": "active"}},
            "instance_identity": {
                "principal_id": "00000000-0000-0000-0000-000000000007",
                "client_id": "00000000-0000-0000-0000-000000000008",
                "token": "NEVER",
            },
            "agent_endpoint": {
                "version_selector": {"version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "unrecognized?NEVER"},
                ]},
                "authorization_schemes": [{"key": "NEVER"}],
            },
        }

    worker = executor(config, get_agent=get_agent)
    records = worker.get_agent_records(include_endpoint=False)
    assert all(record["version"] == "1" and record["status"] == "active"
               and record["agent_endpoint"] is None for record in records.values())
    assert "NEVER" not in json.dumps(records)
    with pytest.raises(PublicationFailure, match="agent_record_selector_version_invalid"):
        worker.get_agent_records()


@pytest.mark.parametrize("selector", ["1", "v1", "1.0.0", "latest", "@latest", "$latest", "*", ">=1.0.0"])
def test_opaque_sdk_selector_is_not_substituted_for_the_observed_version(config, selector):
    worker = executor(config, get_agent=lambda *args: {
        "name": args[-1], "versions": {"latest": {"version": "1", "status": "active"}},
        "agent_endpoint": {"version_selector": {"version_selection_rules": [
            {"type": "FixedRatio", "agent_version": selector},
        ]}},
    })
    records = worker.get_agent_records()
    assert all(record["version"] == "1" for record in records.values())
    assert records["contoso-knowledge-agent"]["agent_endpoint"]["version_selector"][
        "version_selection_rules"
    ][0]["agent_version"] == selector


@pytest.mark.parametrize("scheme,port,expected", [
    ("wss", "", "succeeded"), ("wss", ":443", "succeeded"),
    ("wss", ":444", "failed"), ("ws", "", "failed"),
])
def test_secure_websocket_cli_outputs_keep_their_protocol_and_project(config, scheme, port, expected):
    base = successful_command(config, [])

    def command(argv, *, cwd, env, timeout):
        result = base(argv, cwd=cwd, env=env, timeout=timeout)
        service = argv[2]
        prefix = "AGENT_" + service.upper().replace("-", "_") + "_"
        project = config.project_endpoint.replace("https://", scheme + "://", 1)
        hostname, path = project.split("/api/projects/", 1)
        endpoint = hostname + port + "/api/projects/" + path + f"/agents/{service}/endpoint/protocols/invocations_ws?api-version=v1"
        with (cwd / ".azure" / config.environment / ".env").open("a", encoding="utf-8") as output:
            output.write(f'{prefix}INVOCATIONS_WS_ENDPOINT="{endpoint}"\n')
        return result

    job = executor(config, command=command).publish_service(
        "contoso-knowledge-agent", process_user_agent="inline-test",
    )
    assert job.status == expected
    if expected == "succeeded":
        assert job.services["contoso-knowledge-agent"]["endpoints"]["INVOCATIONS_WS"].startswith("wss://")
    else:
        assert job.error_code == "deployment_endpoint_mismatch"


def staged_config(config):
    from contoso.publication.executor import _EXTENSION_IDS
    stage = config.package_root / "extension-setup"
    (stage / "artifacts").mkdir(parents=True)
    entries, artifacts, versions = [], [], []
    for identifier in sorted(_EXTENSION_IDS):
        version = "1.0.0-beta.99"
        versions.append((identifier, version))
        release = {"version": version, "requiredAzdVersion": ">=1.37.0"}
        if identifier == "microsoft.foundry":
            release["dependencies"] = [
                {"id": dependency, "version": "1.0.0-beta.99"}
                for dependency in sorted(_EXTENSION_IDS - {identifier})
            ]
        else:
            entrypoint = identifier.replace(".", "-") + "-linux-amd64"
            archive = "artifacts/" + entrypoint + ".tar.gz"
            content = ("local fixture " + identifier).encode()
            (stage / archive).write_bytes(content)
            artifacts.append(("extension-setup/" + archive, sha(content)))
            release["artifacts"] = {"linux/amd64": {
                "url": archive, "entryPoint": entrypoint,
                "checksum": {"algorithm": "sha256", "value": sha(content)},
            }}
        entries.append({"id": identifier, "versions": [release]})
    registry = stage / "registry.json"
    registry.write_text(json.dumps({"extensions": entries}))
    setup = ExtensionSetup(
        "extension-setup/registry.json", sha(registry.read_bytes()),
        tuple(artifacts), tuple(versions),
    )
    return replace(config, extension_files=(), extension_setup=setup)


def installer(config, calls):
    def run(argv, *, cwd, env, timeout):
        calls.append(argv)
        assert argv[:4] == [str(cwd / "bin" / "azd"), "extension", "install", "microsoft.foundry"]
        assert argv[-1] == "--no-prompt" and timeout == 210
        assert "AZD_AUTH_KEY" not in env
        assert Path(env["TMPDIR"]).is_relative_to(cwd)
        manifest = json.loads(Path(argv[5]).read_text())
        assert all(not release["artifacts"]["linux/amd64"]["url"].startswith("http")
                   for entry in manifest["extensions"]
                   for release in entry["versions"] if "artifacts" in release)
        installed = {}
        for identifier, version in config.extension_setup.versions:
            entry = {"id": identifier, "version": version, "path": ""}
            if identifier != "microsoft.foundry":
                relative = Path("extensions") / identifier / (identifier.replace(".", "-") + "-linux-amd64")
                target = Path(env["AZD_CONFIG_DIR"]) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"installed fixture")
                entry["path"] = str(relative)
            installed[identifier] = entry
        (Path(env["AZD_CONFIG_DIR"]) / "config.json").write_text(
            json.dumps({"extension": {"installed": installed}}),
        )
        return 0
    return run


def test_official_offline_installer_is_explicit_and_one_service_stays_one_command(config):
    configured = staged_config(config)
    calls = []
    worker = executor(configured, command=installer(configured, calls))
    prepared = worker.prepare_extensions(process_user_agent="inline-test")
    assert prepared["status"] == "prepared"
    assert len(prepared["versions"]) == 8 and len(calls) == 1
    worker.prepare_extensions(process_user_agent="inline-test")
    assert len(calls) == 1
    deploys = []
    worker.command = successful_command(configured, deploys)
    job = worker.publish_service("contoso-knowledge-agent", process_user_agent="inline-test")
    assert job.status == "succeeded" and len(deploys) == 1
    assert worker._prepared_root().exists()


def test_staged_extensions_must_be_prepared_not_auto_installed_in_deploy(config):
    configured = staged_config(config)
    calls = []
    job = executor(configured, command=lambda *a, **k: calls.append(a)).publish_service(
        "contoso-knowledge-agent", process_user_agent="inline-test",
    )
    assert job.error_code == "extensions_not_prepared" and calls == []


def test_staged_registry_rejects_network_artifact_even_when_hash_approved(config):
    configured = staged_config(config)
    path = configured.package_root / configured.extension_setup.registry_relative_path
    manifest = json.loads(path.read_text())
    for entry in manifest["extensions"]:
        if entry["id"] != "microsoft.foundry":
            entry["versions"][0]["artifacts"]["linux/amd64"]["url"] = "https://evil.example/executable.tar.gz"
            break
    path.write_text(json.dumps(manifest))
    configured = replace(configured, extension_setup=replace(
        configured.extension_setup, registry_sha256=sha(path.read_bytes()),
    ))
    calls = []
    with pytest.raises(PublicationFailure, match="extension_registry_invalid"):
        executor(configured, command=lambda *a, **k: calls.append(a)).prepare_extensions(
            process_user_agent="inline-test",
        )
    assert calls == []


def test_installer_nonzero_does_not_leave_prepared_success(config):
    configured = staged_config(config)
    worker = executor(configured, command=lambda *a, **k: 17)
    with pytest.raises(PublicationFailure, match="extension_install_nonzero_exit"):
        worker.prepare_extensions(process_user_agent="inline-test")
    assert not worker._prepared_root().exists()


def linux_package(config):
    from contoso.publication.executor import _EXTENSION_IDS
    installed, files = {}, []
    azd = config.package_root / config.azd_relative_path
    azd.write_bytes(b"#!/bin/sh\nexit 0\n")
    for identifier in sorted(_EXTENSION_IDS):
        entry = {"id": identifier, "version": "1.0.0-beta.99", "path": ""}
        if identifier != "microsoft.foundry":
            relative = Path("extensions") / identifier / (identifier.replace(".", "-") + "-linux-amd64")
            path = config.package_root / "azd-config" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"#!/bin/sh\nexit 0\n")
            entry["path"] = str(relative)
            files.append((str(Path("azd-config") / relative), sha(path.read_bytes())))
        installed[identifier] = entry
    path = config.package_root / "azd-config" / "config.json"
    path.write_text(json.dumps({"extension": {"installed": installed}}))
    files.append(("azd-config/config.json", sha(path.read_bytes())))
    configured = replace(config, azd_sha256=sha(azd.read_bytes()), extension_files=tuple(files))
    for relative in (configured.azd_relative_path, *(p for p, _ in files)):
        (configured.package_root / relative).chmod(0o644)
    return configured


def record_chmod(monkeypatch):
    changes = {}
    original = Path.chmod

    def chmod(path, mode, *args, **kwargs):
        changes[path] = mode
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", chmod)
    return changes


def package_snapshot(config):
    return {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in config.package_root.rglob("*") if path.is_file()
    }


def assert_snapshot(snapshot):
    assert all((path.read_bytes(), path.stat().st_mode) == state for path, state in snapshot.items())


def test_readonly_package_0644_copies_are_executable_and_source_unchanged(config, monkeypatch):
    configured = linux_package(config)
    snapshot = package_snapshot(configured)
    modes = record_chmod(monkeypatch)
    calls = []
    successful = successful_command(configured, calls)

    def run(argv, *, cwd, **kwargs):
        assert argv[0] == str(cwd / "bin" / "azd")
        assert Path(argv[0]).read_bytes() == snapshot[configured.package_root / "azd"][0]
        assert modes[cwd] == modes[cwd / "bin"] == modes[cwd / "azd-config"] == 0o700
        assert modes[Path(argv[0])] == 0o755
        assert modes[cwd / "azd-config" / "config.json"] == 0o644
        for relative, pin in configured.extension_files:
            target = cwd / relative
            assert sha(target.read_bytes()) == pin
            assert modes[target] == (0o644 if target.name == "config.json" else 0o755)
        assert_snapshot(snapshot)
        return successful(argv, cwd=cwd, **kwargs)

    job = executor(configured, command=run).publish(process_user_agent="inline-test")
    assert job.status == "succeeded" and len(calls) == 3
    assert_snapshot(snapshot)
    assert not any(path.is_relative_to(configured.package_root) for path in modes)


@pytest.mark.skipif(os.name != "posix", reason="Native Linux executable mode validation requires POSIX")
def test_copied_linux_executables_run_with_packaged_0644_modes(config):
    configured = linux_package(config)
    snapshot = package_snapshot(configured)
    calls = []
    successful = successful_command(configured, calls)

    def run(argv, *, cwd, **kwargs):
        executables = [Path(argv[0])] + [
            cwd / relative for relative, _ in configured.extension_files
            if Path(relative).name != "config.json"
        ]
        for path in executables:
            assert path.stat().st_mode & 0o777 == 0o755
            subprocess.run([str(path)], cwd=cwd, check=True, timeout=5, capture_output=True)
        assert (cwd / "azd-config" / "config.json").stat().st_mode & 0o777 == 0o644
        return successful(argv, cwd=cwd, **kwargs)

    job = executor(configured, command=run).publish_service("contoso-knowledge-agent", process_user_agent="inline-test")
    assert job.status == "succeeded"
    assert_snapshot(snapshot)


@pytest.mark.parametrize("relative", [
    "bin/azd", "azd-config/extensions/azure.ai.agents/azure-ai-agents-linux-amd64",
])
def test_copied_bytes_rechecked_before_chmod_and_execution(config, monkeypatch, relative):
    configured = linux_package(config)
    snapshot = package_snapshot(configured)
    original = shutil.copyfile
    calls = []

    def copy(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if Path(target).is_relative_to(configured.workspace_root) and Path(target).parts[-len(Path(relative).parts):] == Path(relative).parts:
            Path(target).write_bytes(b"changed copied binary")
        return result

    monkeypatch.setattr(shutil, "copyfile", copy)
    job = executor(configured, command=lambda *a, **k: calls.append(a)).publish_service(
        "contoso-knowledge-agent", process_user_agent="inline-test",
    )
    assert job.status == "failed" and job.error_code in {"binary_hash_mismatch", "extension_hash_mismatch"}
    assert calls == []
    assert_snapshot(snapshot)


@pytest.mark.parametrize("relative", [
    "bin/azd", "azd-config/extensions/azure.ai.agents/azure-ai-agents-linux-amd64",
])
def test_executable_digest_rechecked_immediately_before_command(config, relative):
    configured = linux_package(config)
    calls = []

    @contextmanager
    def change_after_copy(workspace, credential, tenant, scopes):
        (workspace / relative).write_bytes(b"changed after initial verification")
        with auth(workspace, credential, tenant, scopes) as env:
            yield env

    worker = executor(configured, command=lambda *a, **k: calls.append(a))
    worker.auth_context = change_after_copy
    job = worker.publish_service("contoso-knowledge-agent", process_user_agent="inline-test")
    assert job.error_code == "executable_hash_mismatch" and calls == []


def test_executable_digest_rechecked_between_services(config):
    configured = linux_package(config)
    calls = []
    successful = successful_command(configured, calls)

    def run(argv, *, cwd, **kwargs):
        result = successful(argv, cwd=cwd, **kwargs)
        Path(argv[0]).write_bytes(b"changed between deployments")
        return result

    job = executor(configured, command=run).publish(process_user_agent="inline-test")
    assert job.error_code == "executable_hash_mismatch"
    assert len(calls) == 1 and set(job.services) == {configured.services[0]}


@pytest.mark.parametrize("relative", [
    "../../azd", "extensions/azure.ai.agents/config.json",
    "extensions/unknown/azure-ai-agents-linux-amd64",
    "extensions/azure.ai.agents/arbitrary-linux-amd64",
])
def test_installed_config_cannot_select_unknown_executable(config, relative):
    configured = linux_package(config)
    path = configured.package_root / "azd-config" / "config.json"
    contents = json.loads(path.read_text())
    contents["extension"]["installed"]["azure.ai.agents"]["path"] = relative
    path.write_text(json.dumps(contents))
    configured = replace(configured, extension_files=tuple(
        (name, sha(path.read_bytes()) if Path(name).name == "config.json" else pin)
        for name, pin in configured.extension_files
    ))
    calls = []
    job = executor(configured, command=lambda *a, **k: calls.append(a)).publish_service(
        "contoso-knowledge-agent", process_user_agent="inline-test",
    )
    assert job.error_code == "extension_executable_path_invalid" and calls == []


def test_offline_setup_uses_chmodded_azd_copy_and_config_is_nonexecutable(config, monkeypatch):
    configured = staged_config(config)
    for path in configured.package_root.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    snapshot = package_snapshot(configured)
    modes = record_chmod(monkeypatch)
    calls = []
    install = installer(configured, calls)

    def run(argv, *, cwd, **kwargs):
        assert argv[0] == str(cwd / "bin" / "azd")
        assert modes[Path(argv[0])] == 0o755
        assert sha(Path(argv[0]).read_bytes()) == configured.azd_sha256
        assert_snapshot(snapshot)
        return install(argv, cwd=cwd, **kwargs)

    worker = executor(configured, command=run)
    prepared = worker.prepare_extensions(process_user_agent="inline-test")
    root = worker._prepared_root()
    for relative, pin in prepared["files"].items():
        path = root / "azd-config" / relative
        assert sha(path.read_bytes()) == pin
        assert modes[path] == (0o755 if path.name.endswith("-linux-amd64") else 0o644)
    prepared_snapshot = {
        root / "azd-config" / relative: ((root / "azd-config" / relative).read_bytes(),
                                       (root / "azd-config" / relative).stat().st_mode)
        for relative in prepared["files"]
    }
    deploys = []
    worker.command = successful_command(configured, deploys)
    job = worker.publish_service("contoso-knowledge-agent", process_user_agent="inline-test")
    assert job.status == "succeeded" and len(calls) == len(deploys) == 1
    assert_snapshot(snapshot)
    assert_snapshot(prepared_snapshot)


def test_offline_setup_rejects_unknown_fixed_entrypoint(config):
    configured = staged_config(config)
    path = configured.package_root / configured.extension_setup.registry_relative_path
    registry = json.loads(path.read_text())
    entry = next(e for e in registry["extensions"] if e["id"] == "azure.ai.agents")
    entry["versions"][0]["artifacts"]["linux/amd64"]["entryPoint"] = "azure-ai-arbitrary-linux-amd64"
    path.write_text(json.dumps(registry))
    configured = replace(configured, extension_setup=replace(
        configured.extension_setup, registry_sha256=sha(path.read_bytes()),
    ))
    calls = []
    with pytest.raises(PublicationFailure, match="extension_registry_invalid"):
        executor(configured, command=lambda *a, **k: calls.append(a)).prepare_extensions(
            process_user_agent="inline-test",
        )
    assert calls == []
