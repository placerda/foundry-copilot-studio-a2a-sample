"""Package only reviewed runtime code and hash-pinned publication assets."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import zipfile


def stage_gateway(package: Path, output: Path):
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((package / "manifest.json").read_bytes())
    if set(manifest["documents"]) != {
        "knowledge-returnables.md", "supplier-delivery-terms.md",
    }:
        raise ValueError("Only the two approved knowledge documents can be packaged")
    extension_names = {
        "azd-config/extensions/azure.ai." + name + "/azure-ai-" + name + "-linux-amd64"
        for name in ("agents", "connections", "inspector", "projects", "routines", "skills", "toolboxes")
    }
    if {row[0] for row in manifest["extension_files"]} != extension_names | {"azd-config/config.json"}:
        raise ValueError("Only the pinned Linux extension set can be packaged")
    pinned = [
        ("tools/azd", manifest["azd_sha256"]),
        ("azure.yaml", manifest["azure_yaml_sha256"]),
        *manifest["extension_files"],
        *[
            ("knowledge/" + name, digest)
            for name, digest in manifest["documents"].items()
        ],
    ]
    entries = {
        "function_app.py": (root / "contoso" / "function_app.py", False),
        "host.json": (root / "contoso" / "host.json", False),
        "requirements.txt": (root / "contoso" / "requirements.txt", False),
        "publication-package/manifest.json": (package / "manifest.json", False),
        "contoso-demo/scenarios.json": (root / "contoso-demo" / "scenarios.json", False),
    }
    for relative, digest in pinned:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("A publication asset requires its SHA256 digest")
        path = package.joinpath(*relative.split("/"))
        if path.is_symlink() or not path.is_file():
            raise ValueError("Pinned publication asset is not a regular file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Pinned publication asset digest changed: " + relative)
        entries["publication-package/" + relative] = (
            path, relative == "tools/azd" or relative.startswith("azd-config/extensions/"),
        )
    for folder in ("contoso", "contoso_backend"):
        for source in (root / folder).rglob("*.py"):
            relative = source.relative_to(root)
            if any(part in ("tests", "ops", "__pycache__", ".venv") for part in relative.parts):
                continue
            if source == root / "contoso" / "function_app.py":
                continue
            entries[relative.as_posix()] = (source, False)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, (path, executable) in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 3, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if executable else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return {
        "files": len(entries),
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage_gateway(args.package, args.output)))
