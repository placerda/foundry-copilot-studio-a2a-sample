import base64
import io
import json
from pathlib import Path
import shlex
import zipfile

import yaml

from contoso.ops.create_image import source_archive, task_content


def test_reviewed_image_context_is_reproducible_and_excludes_credentials():
    root = Path(__file__).resolve().parents[2]
    source = source_archive(root)
    assert source == source_archive(root)
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        names = archive.namelist()
        assert "contoso/entrypoint.py" in names
        assert "contoso/identity_probe.py" in names
        assert "contoso/api_center.py" in names
        assert "contoso/router/native_a2a.py" in names
        assert "contoso/router/hosted.py" in names
        assert "contoso/router/discovery.py" in names
        assert "contoso/providers/diagnostics.py" in names
        assert "contoso-demo/scenarios.json" in names
        assert "contoso-demo/motors-scenarios.json" in names
        assert not any(
            part in name for name in names
            for part in (".env", ".azure", ".venv", "tests/", "__pycache__", "config.js", ".pem")
        )
        assert not any(name.startswith("contoso/gateway/") or name.startswith("contoso/publication/") for name in names)


def test_catalog_probe_dependency_is_copied_into_the_image():
    root = Path(__file__).resolve().parents[2]
    with zipfile.ZipFile(io.BytesIO(source_archive(root))) as archive:
        dockerfile = archive.read("contoso/Dockerfile").decode("utf-8")
        runtime_sources = [
            source
            for line in dockerfile.splitlines() if line.startswith("COPY ")
            for source in shlex.split(line)[1:-1]
        ]
        assert "contoso/api_center.py" in runtime_sources
        assert archive.read("contoso/api_center.py")
        assert "contoso-demo/motors-scenarios.json" in runtime_sources
        assert archive.read("contoso-demo/motors-scenarios.json")


def test_encoded_task_uses_managed_registry_and_fixed_image_repository():
    recipe = json.loads(base64.b64decode(task_content(b"source", "a" * 64)))
    assert recipe["version"] == "v1.1.0"
    assert len(recipe["steps"]) == 3
    assert recipe["steps"][2] == {"push": ["{{.Run.Registry}}/contoso-runtime:" + "a" * 64]}


def test_router_manifest_preserves_native_and_passthrough_protocols():
    root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / "contoso" / "azure.yaml").read_text(encoding="utf-8"))
    service = manifest["services"]["contoso-foundry-agent"]
    protocols = {item["protocol"]: item["version"] for item in service["protocols"]}
    assert protocols == {"responses": "2.0.0", "a2a": "1.0.0", "invocations": "2.0.0"}
    assert set(service["agentEndpoint"]["protocols"]) == set(protocols)
    assert service["image"] == "${CONTOSO_RUNTIME_IMAGE}"
    assert service["env"]["CONTOSO_RUNTIME_ROLE"] == "${CONTOSO_ROUTER_RUNTIME_ROLE}"
    assert service["agentEndpoint"]["versionSelector"]["versionSelectionRules"] == [
        {"type": "FixedRatio", "agentVersion": "4", "trafficPercentage": 100},
    ]
