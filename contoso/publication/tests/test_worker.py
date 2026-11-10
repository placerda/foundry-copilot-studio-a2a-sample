import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from azure.core.exceptions import AzureError
import pytest
from requests.exceptions import RequestException

from contoso.publication import PublicationFailure
from contoso.publication import runtime, worker


AGENT = "contoso-foundry-agent"
USER_AGENT = "contoso-private-operator/1.0"


@pytest.mark.parametrize("argv,method,call_args,call_kwargs,expected", [
    (["preflight"], "preflight", (), {}, {"status": "private-preflight-passed"}),
    (["sources"], "publish_sources", (), {}, {"document": "safe-etag"}),
    (["records"], "get_agent_records", (), {}, {"agent": "safe-metadata"}),
    (["metadata"], "get_agent_records", (), {"include_endpoint": False}, {
        "record_contract": 1, "endpoint_validation": "not_requested",
        "records": {"agent": "safe-metadata"},
    }),
    (["diagnostics", "--process-user-agent", USER_AGENT],
     "diagnostic_self_test", (), {"process_user_agent": USER_AGENT}, {"status": "passed"}),
    (["publish-service", "--service", AGENT, "--process-user-agent", USER_AGENT],
     "publish_service", (AGENT,), {"process_user_agent": USER_AGENT}, {"status": "succeeded"}),
])
def test_fixed_dispatch(argv, method, call_args, call_kwargs, expected, capsys):
    executor = Mock()
    executor.publish_sources.return_value = {"document": "safe-etag"}
    executor.get_agent_records.return_value = {"agent": "safe-metadata"}
    executor.diagnostic_self_test.return_value = {"status": "passed"}
    executor.publish_service.return_value.as_dict.return_value = {"status": "succeeded"}
    factory = Mock(return_value=executor)
    assert worker.main(argv, executor_factory=factory) == 0
    factory.assert_called_once_with()
    getattr(executor, method).assert_called_once_with(*call_args, **call_kwargs)
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize("argv", [
    [], ["arbitrary"], ["publish-service"], ["diagnostics"],
    ["preflight", "--service", AGENT],
    ["records", "--endpoint", "https://attacker.example"],
    ["publish-service", "--service", "unapproved", "--process-user-agent", USER_AGENT],
    ["diagnostics", "--process-user-agent", "x\ninjected"],
    ["diagnostics", "--process-user-agent", ""],
    ["diagnostics", "--process-user-agent", "x" * 257],
    ["diagnostics", "--process-user", USER_AGENT],
])
def test_invalid_input_never_constructs_executor(argv):
    factory = Mock()
    with pytest.raises(SystemExit) as error:
        worker.main(argv, executor_factory=factory)
    assert error.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("operation", ["diagnostics", "publish-service"])
def test_failure_result_has_nonzero_exit(operation, capsys, caplog):
    executor = Mock()
    result = {"status": "failed", "error_code": "approved_failure"}
    executor.diagnostic_self_test.return_value = result
    executor.publish_service.return_value.as_dict.return_value = result
    argv = [operation, "--process-user-agent", USER_AGENT]
    if operation == "publish-service":
        argv.extend(["--service", AGENT])
    assert worker.main(argv, executor_factory=lambda: executor) == 1
    assert json.loads(capsys.readouterr().out) == result
    assert "private publication worker failed" in caplog.text


@pytest.mark.parametrize("error,code", [
    (PublicationFailure("gateway_system_identity_required"), "gateway_system_identity_required"),
    (AzureError("private-upstream-secret"), "private_dependency_unavailable"),
    (RequestException("private-upstream-secret"), "private_dependency_unavailable"),
    (ValueError("private-upstream-secret"), "invalid_publication_configuration_or_operation"),
    (KeyError("private-upstream-secret"), "invalid_publication_configuration_or_operation"),
    (OSError("private-upstream-secret"), "invalid_publication_configuration_or_operation"),
])
def test_errors_are_explicit_and_do_not_leak_dependency_details(error, code, capsys, caplog):
    factory = Mock(side_effect=error)
    assert worker.main(["preflight"], executor_factory=factory) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "failed", "error": code}
    assert "private-upstream-secret" not in output + caplog.text


def test_default_factory_uses_only_trusted_environment(monkeypatch, capsys):
    config = object()
    supplied = {"trusted": "configuration"}
    configure = Mock(return_value=config)
    executor = Mock()
    create = Mock(return_value=executor)
    monkeypatch.setattr(worker, "operator_config", configure)
    monkeypatch.setattr(worker, "create_executor", create)
    assert worker.main(["preflight"], env=supplied) == 0
    configure.assert_called_once_with(supplied)
    create.assert_called_once_with(config)
    capsys.readouterr()


def test_worker_import_does_not_load_functions_host():
    script = (
        "import sys; import contoso.publication.worker; "
        "assert 'azure.functions' not in sys.modules; "
        "assert 'contoso.gateway_bootstrap' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30, capture_output=True)


def test_shared_job_store_uses_executor_identity_and_fixed_blob(monkeypatch):
    config = SimpleNamespace(blob_endpoint="https://approved.blob.core.windows.net")
    credential = object()
    captured = {}
    executor = SimpleNamespace(credential=credential)

    def production(actual, *, job_store):
        assert actual is config
        captured["persist"] = job_store
        return executor

    blob = Mock()
    blob.__enter__ = Mock(return_value=blob)
    blob.__exit__ = Mock(return_value=False)
    blob_factory = Mock(return_value=blob)
    monkeypatch.setattr(runtime, "production_executor", production)
    monkeypatch.setattr(runtime, "BlobClient", blob_factory)
    assert runtime.create_executor(config) is executor
    metadata = {"job_id": "a" * 32, "status": "running"}
    captured["persist"](metadata)
    assert blob_factory.call_args.kwargs == {
        "account_url": config.blob_endpoint, "container_name": "sources",
        "blob_name": "deployment-jobs/" + "a" * 32 + ".json", "credential": credential,
        "connection_timeout": 5, "read_timeout": 15, "retry_total": 0,
    }
    args, kwargs = blob.upload_blob.call_args
    assert json.loads(args[0]) == metadata
    assert kwargs["overwrite"] is True
    assert kwargs["content_settings"].content_type == "application/json"
    blob_factory.reset_mock()
    for invalid in ({"job_id": "../unapproved"}, {"job_id": "a" * 32, "large": "x" * 32768}):
        with pytest.raises(PublicationFailure):
            captured["persist"](invalid)
    blob_factory.assert_not_called()
