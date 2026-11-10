"""Pinned publication configuration and durable jobs, independent of Functions."""

import hashlib
import json
from pathlib import Path
import re

from azure.storage.blob import BlobClient, ContentSettings

from contoso.configuration import required
from .executor import Document, OperatorConfig, PublicationFailure, production_executor
from .operator_settings import publication_environment, publication_settings, publication_workspace


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate publication field")
        result[key] = value
    return result


def operator_config(env, *, package=None):
    package = package or Path(__file__).resolve().parents[2] / "publication-package"
    manifest_bytes = (package / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != required(env, "CONTOSO_PUBLICATION_MANIFEST_SHA256"):
        raise PublicationFailure("publication_manifest_changed")
    manifest = json.loads(manifest_bytes, object_pairs_hook=unique_fields)
    bindings = json.loads(required(env, "CONTOSO_PUBLICATION_BINDINGS"), object_pairs_hook=unique_fields)
    override_path = package / "operator-settings.json"
    overrides = json.loads(override_path.read_bytes(), object_pairs_hook=unique_fields) if override_path.exists() else None
    settings = publication_settings(bindings, env, overrides)
    return OperatorConfig(
        package_root=package,
        workspace_root=publication_workspace(settings),
        azure_yaml_sha256=manifest["azure_yaml_sha256"],
        azd_relative_path="tools/azd",
        azd_sha256=manifest["azd_sha256"],
        extension_files=tuple(tuple(row) for row in manifest["extension_files"]),
        services=("contoso-knowledge-agent", "contoso-foundry-sdk-agent", "contoso-foundry-agent"),
        image=bindings["image"],
        tenant_id=bindings["tenant_id"],
        subscription_id=bindings["subscription_id"],
        resource_group=bindings["resource_group"],
        project_id=bindings["project_id"],
        project_endpoint=bindings["project_endpoint"],
        registry_endpoint=bindings["registry_endpoint"],
        registry_id=bindings["registry_id"],
        blob_endpoint=bindings["blob_endpoint"],
        source_storage_id=bindings["source_storage_id"],
        environment="contoso-demo",
        documents=tuple(
            Document(
                name=name, relative_path="knowledge/" + name,
                sha256=manifest["documents"][name],
            )
            for name in ("knowledge-returnables.md", "supplier-delivery-terms.md")
        ),
        environment_values=publication_environment(bindings, settings),
    )


def create_executor(config):
    def persist(metadata):
        job_id = metadata.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise PublicationFailure("invalid_publication_job_id")
        data = json.dumps(metadata, ensure_ascii=True).encode()
        if len(data) > 32768:
            raise PublicationFailure("publication_metadata_too_large")
        with BlobClient(
            account_url=config.blob_endpoint, container_name="sources",
            blob_name="deployment-jobs/" + job_id + ".json", credential=executor.credential,
            connection_timeout=5, read_timeout=15, retry_total=0,
        ) as blob:
            blob.upload_blob(
                data, overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
            )

    executor = production_executor(config, job_store=persist)
    return executor
