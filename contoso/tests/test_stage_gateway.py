import hashlib
import json
import stat
import zipfile

import pytest

from contoso.ops.stage_gateway import stage_gateway


def package(root):
    files = {
        "tools/azd": b"linux-azd",
        "azure.yaml": b"reviewed-service-definition",
        "azd-config/config.json": b'{"extension":{"installed":[]}}',
        "knowledge/knowledge-returnables.md": b"synthetic knowledge",
        "knowledge/supplier-delivery-terms.md": b"synthetic consumer",
    }
    for name in ("agents", "connections", "inspector", "projects", "routines", "skills", "toolboxes"):
        files[f"azd-config/extensions/azure.ai.{name}/azure-ai-{name}-linux-amd64"] = name.encode()
    for name, data in files.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    digest = lambda name: hashlib.sha256(files[name]).hexdigest()
    manifest = {
        "azd_sha256": digest("tools/azd"),
        "azure_yaml_sha256": digest("azure.yaml"),
        "extension_files": [
            [name, digest(name)] for name in files if name.startswith("azd-config/")
        ],
        "documents": {
            name: digest("knowledge/" + name)
            for name in ("knowledge-returnables.md", "supplier-delivery-terms.md")
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_exact_runtime_layout_pins_and_linux_executable_modes(tmp_path):
    assets = tmp_path / "assets"
    package(assets)
    (assets / "unexpected-auth.json").write_text("must-not-ship")
    archive_path = tmp_path / "gateway.zip"
    result = stage_gateway(assets, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {"function_app.py", "host.json", "requirements.txt", "contoso/__init__.py"} <= names
        assert "contoso/gateway/function_app.py" in names
        assert "contoso/function_app.py" not in names
        assert not any("tests/" in name or "ops/" in name or ".venv/" in name for name in names)
        assert not any("unexpected-auth" in name for name in names)
        for name in names:
            mode = archive.getinfo(name).external_attr >> 16
            assert stat.S_ISREG(mode)
            executable = name == "publication-package/tools/azd" or "/extensions/" in name
            assert stat.S_IMODE(mode) == (0o755 if executable else 0o644)
        assert result["files"] == len(names)
    assert result["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_changed_pinned_binary_is_not_packaged(tmp_path):
    package(tmp_path)
    (tmp_path / "tools" / "azd").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest changed"):
        stage_gateway(tmp_path, tmp_path / "rejected.zip")


@pytest.mark.parametrize("field,path", [
    ("documents", "../../auth.json"),
    ("extension_files", "azd-config/../../auth.json"),
])
def test_manifest_cannot_select_unapproved_or_parent_files(tmp_path, field, path):
    manifest = package(tmp_path)
    if field == "documents":
        manifest[field][path] = "0" * 64
    else:
        manifest[field].append([path, "0" * 64])
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        stage_gateway(tmp_path, tmp_path / "rejected.zip")
