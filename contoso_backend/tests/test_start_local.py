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
    "CONTOSO_REQUESTER_CLIENT_SECRET", "CONTOSO_REQUESTER_PRIVATE_KEY",
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


def requester_config(**changes):
    return {
        "TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "API_AUDIENCE": "22222222-2222-2222-2222-222222222222",
        "ALLOWED_USER_OID": "33333333-3333-3333-3333-333333333333",
        "CONTOSO_ENTRA_TENANT_ID": "44444444-4444-4444-4444-444444444444",
        "CONTOSO_GATEWAY_API_CLIENT_ID": "55555555-5555-5555-5555-555555555555",
        "CONTOSO_REQUESTER_CLIENT_ID": "66666666-6666-6666-6666-666666666666",
        "CONTOSO_GATEWAY_URL": "https://sample-apim.azure-api.net/api/a2a",
        **changes,
    }


@pytest.mark.parametrize("source", ["environment", "automatic", "WINDOWS", "WINDOWS-CERTIFICATE"])
def test_launcher_rejects_unprotected_or_unknown_credential_modes(workspace, source):
    write_config(workspace, requester_config(CONTOSO_REQUESTER_CREDENTIAL_SOURCE=source))
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "supports only windows or windows-certificate" in result.stderr


def test_launcher_requires_certificate_path_for_certificate_mode(workspace):
    write_config(workspace, requester_config(CONTOSO_REQUESTER_CREDENTIAL_SOURCE="windows-certificate"))
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "Missing configuration field: CONTOSO_REQUESTER_CERTIFICATE_PATH" in result.stderr


def test_required_public_settings_are_checked_before_certificate_options(workspace):
    write_config(workspace, {
        "TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "CONTOSO_REQUESTER_CREDENTIAL_SOURCE": "windows-certificate",
    })
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "Missing configuration field: API_AUDIENCE" in result.stderr


def test_launcher_does_not_allow_certificate_path_with_legacy_secret(workspace):
    write_config(workspace, requester_config(CONTOSO_REQUESTER_CERTIFICATE_PATH="C:\\public.pem"))
    result = run_launcher(workspace)
    assert result.returncode != 0
    assert "public certificate path requires windows-certificate" in result.stderr
