import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL,
    reason="The launcher requires Windows PowerShell and Credential Manager",
)


@pytest.fixture
def workspace(tmp_path):
    shutil.copyfile(ROOT / "start-local.ps1", tmp_path / "start-local.ps1")
    return tmp_path


def run_launcher(workspace):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
         str(workspace / "start-local.ps1"), "-CheckOnly"],
        capture_output=True, text=True, timeout=30,
    )


def write_config(workspace, value):
    (workspace / "local-demo.config.json").write_text(
        json.dumps(value), encoding="utf-8",
    )


def test_missing_configuration_has_actionable_error(workspace):
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "Copy local-demo.config.example.json" in result.stderr


@pytest.mark.parametrize("value", [None, [], [{}], "not-an-object", 17])
def test_configuration_requires_an_object(workspace, value):
    write_config(workspace, value)
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "JSON object" in result.stderr


def test_malformed_configuration_has_sanitized_error(workspace):
    marker = "sensitive-test-marker"
    (workspace / "local-demo.config.json").write_text(
        '{"TENANT_ID": "' + marker, encoding="utf-8",
    )
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "valid JSON object" in result.stderr
    assert marker not in result.stderr + result.stdout


@pytest.mark.parametrize("field", [
    "CONTOSO_REQUESTER_CLIENT_SECRET", "CONTOSO_REQUESTER_CREDENTIAL_SOURCE",
    "PYTHONPATH", "tenant_id",
])
def test_unsupported_fields_are_rejected_without_printing_values(workspace, field):
    marker = "sensitive-test-marker"
    write_config(workspace, {field: marker})
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "Unsupported configuration field" in result.stderr
    assert marker not in result.stderr + result.stdout


@pytest.mark.parametrize("value", ["", " ", None, 17, {}, []])
def test_configuration_requires_nonempty_strings(workspace, value):
    write_config(workspace, {"TENANT_ID": value})
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "must be a nonempty string" in result.stderr


def test_required_configuration_is_not_inherited_silently(workspace):
    write_config(workspace, {})
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "Missing configuration field: TENANT_ID" in result.stderr
