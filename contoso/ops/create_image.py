"""Build the reviewed container using a persistent, secretless ACR Task."""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import time
from uuid import UUID, uuid5, NAMESPACE_URL
import zipfile

import requests
from azure.identity import AzureCliCredential


TASK_NAME = "contoso-image-builder"
ACR_API = "2025-03-01-preview"
ARM_SCOPE = "https://management.azure.com/.default"
ACR_PUSH_ROLE = "8311e382-0749-4cb8-b61a-304f252e45ec"


def source_archive(root: Path, *, sdk_proof: bool = False) -> bytes:
    paths = [
        root / ".dockerignore",
        root / "contoso" / "Dockerfile",
        root / "contoso" / "requirements.txt",
        root / "contoso-demo" / "scenarios.json",
    ]
    paths.extend(root / "contoso_backend" / name for name in ("__init__.py", "auth.py", "errors.py"))
    paths.extend(root / "contoso" / name for name in (
        "__init__.py", "knowledge.py", "bounded.py", "configuration.py", "entrypoint.py",
        "identity_probe.py", "identity_link.py", "token_client.py",
    ))
    for directory in ("router", "providers"):
        paths.extend(
            path for path in (root / "contoso" / directory).rglob("*.py")
            if "tests" not in path.parts and "__pycache__" not in path.parts
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            if path.is_symlink() or not path.is_file():
                raise ValueError("The reviewed image source contains an invalid file")
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            if sdk_proof and path == root / "contoso" / "Dockerfile":
                command = b'CMD ["python", "-m", "contoso.entrypoint"]'
                if data.count(command) != 1:
                    raise ValueError("The reviewed runtime command changed")
                data = data.replace(command, b'CMD ["python", "-m", "contoso.providers.sdk_proof"]')
            archive.writestr(info, data)
    payload = buffer.getvalue()
    if len(payload) > 524288:
        raise ValueError("The reviewed image context exceeded its byte limit")
    return payload


def task_content(source: bytes, tag: str) -> str:
    encoded = base64.b64encode(source).decode("ascii")
    prepare = (
        "import base64,io,os,zipfile;os.chdir('/workspace');"
        f"zipfile.ZipFile(io.BytesIO(base64.b64decode('{encoded}'))).extractall('.')"
    )
    task = {
        "version": "v1.1.0",
        "steps": [
            {"cmd": f'python:3.12-slim python -c "{prepare}"'},
            {"build": f"-t {{{{.Run.Registry}}}}/contoso-runtime:{tag} -f contoso/Dockerfile ."},
            {"push": [f"{{{{.Run.Registry}}}}/contoso-runtime:{tag}"]},
        ],
    }
    return base64.b64encode(json.dumps(task, separators=(",", ":")).encode()).decode("ascii")


class Arm:
    def __init__(self, credential):
        self.credential = credential
        self.session = requests.Session()
        self.session.trust_env = False

    def request(self, method, resource_id, *, api_version, payload=None):
        if (not resource_id.startswith("/subscriptions/") or "?" in resource_id
                or any(part in resource_id for part in ("..", "\\", "#", "%"))):
            raise ValueError("A trusted ARM resource path is required")
        token = self.credential.get_token(ARM_SCOPE)
        response = self.session.request(
            method, f"https://management.azure.com{resource_id}?api-version={api_version}",
            headers={"Authorization": f"Bearer {token.token}"},
            json=payload, timeout=(5, 60), allow_redirects=False,
        )
        if response.status_code not in (200, 201, 202):
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            code = error.get("code", "unknown")
            message = str(error.get("message", "No structured ARM error"))[:512]
            raise RuntimeError(
                f"Reviewed ACR operation failed with HTTP {response.status_code}: {code}: {message}"
            )
        return response.json()


def build(arm: Arm, acr_id: str, root: Path, *, sdk_proof: bool = False) -> dict:
    match = re.fullmatch(
        r"/subscriptions/([0-9a-f-]{36})/resourceGroups/(rg-contoso-[a-z0-9-]+)/"
        r"providers/Microsoft.ContainerRegistry/registries/(contoso[a-z0-9]+)", acr_id,
    )
    if match is None:
        raise ValueError("Only the new reviewed CONTOSO registry is permitted")
    subscription = str(UUID(match[1]))
    registry = arm.request("GET", acr_id, api_version=ACR_API)
    if registry["properties"]["publicNetworkAccess"] != "Disabled":
        raise RuntimeError("The reviewed registry must retain disabled public access")
    source = source_archive(root, sdk_proof=sdk_proof)
    tag = hashlib.sha256(source).hexdigest()
    task_id = acr_id + "/tasks/" + TASK_NAME
    task = arm.request("PUT", task_id, api_version=ACR_API, payload={
        "location": registry["location"],
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "status": "Enabled",
            "platform": {"os": "Linux", "architecture": "amd64"},
            "agentConfiguration": {"cpu": 2},
            "timeout": 900,
            "step": {"type": "EncodedTask", "encodedTaskContent": task_content(source, tag)},
            "credentials": {
                "customRegistries": {
                    registry["properties"]["loginServer"]: {"identity": "[system]"},
                },
            },
        },
    })
    principal_id = str(UUID(task["identity"]["principalId"]))
    assignment_id = str(uuid5(NAMESPACE_URL, task_id + "/" + ACR_PUSH_ROLE))
    role_path = acr_id + "/providers/Microsoft.Authorization/roleAssignments/" + assignment_id
    expected_role = {
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
            "roleDefinitionId": (
                f"/subscriptions/{subscription}/providers/Microsoft.Authorization/"
                f"roleDefinitions/{ACR_PUSH_ROLE}"
            ),
    }
    if sdk_proof:
        assigned = arm.request("GET", role_path, api_version="2022-04-01")["properties"]
        if any(assigned.get(key, "").casefold() != value.casefold()
               for key, value in expected_role.items()):
            raise RuntimeError("The isolated SDK build requires the existing exact task AcrPush binding")
    else:
        arm.request("PUT", role_path, api_version="2022-04-01",
                    payload={"properties": expected_role})
    run = arm.request("POST", acr_id + "/scheduleRun", api_version=ACR_API, payload={
        "type": "TaskRunRequest", "taskId": task_id, "isArchiveEnabled": False,
    })
    run_id = run["properties"]["runId"]
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", run_id):
        raise RuntimeError("The registry returned an invalid task run identifier")
    deadline = time.monotonic() + 900
    delay = 10
    while time.monotonic() < deadline:
        details = arm.request("GET", acr_id + "/runs/" + run_id, api_version=ACR_API)
        status = details["properties"]["status"]
        if status == "Succeeded":
            images = details["properties"].get("outputImages", [])
            matching = [
                image for image in images
                if image.get("repository") == "contoso-runtime" and image.get("tag") == tag
            ]
            if len(matching) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", matching[0].get("digest", "")):
                raise RuntimeError("The successful build did not return the reviewed image digest")
            return {
                "task_id": task_id, "task_principal_id": principal_id, "run_id": run_id,
                "status": status, "source_sha256": tag,
                "image": registry["properties"]["loginServer"] + "/contoso-runtime@" + matching[0]["digest"],
            }
        if status in ("Failed", "Canceled", "Error", "Timeout"):
            raise RuntimeError(f"The reviewed image build ended with {status}, run {run_id}")
        time.sleep(delay)
        delay = min(delay * 2, 40)
    raise RuntimeError(f"The reviewed image build exceeded its budget, run {run_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--acr-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--sdk-proof", action="store_true")
    args = parser.parse_args()
    credential = AzureCliCredential(tenant_id=str(UUID(args.tenant_id)))
    result = build(Arm(credential), args.acr_id, Path(__file__).resolve().parents[2],
                   sdk_proof=args.sdk_proof)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
