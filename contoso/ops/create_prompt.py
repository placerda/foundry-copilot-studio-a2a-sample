"""Prepare the separate Motors Prompt Agent or explicitly enable its approved A2A card."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID

import requests
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, Reasoning
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential


AGENT_NAME = "contoso-motors-logistics"
ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("contoso-demo") / "knowledge" / "motors-logistics-returnables-v1.md"
INSTRUCTIONS = Path("contoso-demo") / "prompts" / "motors-logistics-v1.md"
CARD = Path("contoso-demo") / "prompts" / "motors-logistics-v1.card.json"


def build_definition(model_deployment: str, *, root: Path = ROOT) -> PromptAgentDefinition:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model_deployment):
        raise ValueError("An explicit existing model deployment name is required")
    documents = []
    for relative in (INSTRUCTIONS, SOURCE):
        path = root / relative
        if path.is_symlink() or not 0 < path.stat().st_size <= 32768:
            raise ValueError("The reviewed Prompt material has an invalid size or path")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("The reviewed Prompt material cannot be empty")
        documents.append(text.strip())
    source = documents[1]
    required = (
        "Source ID: CONTOSO-MOTORS-LOGISTICS-001", "Version: 1",
        "## Receiving coordination", "## Returnable packaging process",
        "## Recommended next step", "## Evidence limits",
    )
    if any(line not in source.splitlines() for line in required):
        raise ValueError("The embedded reference does not match the reviewed citation contract")
    return PromptAgentDefinition(
        model=model_deployment,
        instructions=documents[0] + "\n\n# Embedded synthetic reference\n\n" + source + "\n",
        reasoning=Reasoning(effort="minimal"),
    )


def definition_metadata(definition: PromptAgentDefinition) -> dict[str, str]:
    encoded = json.dumps(
        definition.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return {
        "contoso-purpose": "motors-logistics-v1",
        "contoso-definition-sha256": hashlib.sha256(encoded).hexdigest(),
    }


def create_or_verify(client, definition: PromptAgentDefinition) -> dict[str, str]:
    status = "verified-existing"
    try:
        existing = client.agents.get(AGENT_NAME).versions.latest
    except ResourceNotFoundError:
        created = client.agents.create_version(
            AGENT_NAME, definition=definition, metadata=definition_metadata(definition),
        )
        existing = client.agents.get_version(AGENT_NAME, created.version)
        status = "created"
    if (existing is None or existing.name != AGENT_NAME or not existing.version
            or existing.definition.as_dict() != definition.as_dict()):
        raise RuntimeError("The fixed Prompt name has a different or unverified definition")
    return {
        "status": status, "agent_name": AGENT_NAME, "version": existing.version,
        **definition_metadata(definition),
    }


def enable_or_verify_a2a(client, definition: PromptAgentDefinition) -> dict:
    card = json.loads((ROOT / CARD).read_text(encoding="utf-8"))
    before = client.agents.get(AGENT_NAME).as_dict()
    version = client.agents.get_version(AGENT_NAME, "1")
    endpoint = before.get("agent_endpoint", {})
    protocols = endpoint.get("protocols")
    configuration = endpoint.get("protocol_configuration")
    authorization = [{"type": "Entra", "isolation_key_source": {"kind": "Entra"}}]
    selector = {"version_selection_rules": [
        {"type": "FixedRatio", "agent_version": "1", "traffic_percentage": 100},
    ]}
    latest_selector = deepcopy(selector)
    latest_selector["version_selection_rules"][0]["agent_version"] = "@latest"
    if (before.get("name") != AGENT_NAME or before.get("state") != "enabled"
            or before.get("versions", {}).get("latest", {}).get("version") != "1"
            or version.definition.as_dict() != definition.as_dict()
            or before.get("agent_card") not in (None, card)
            or protocols not in (["responses"], ["responses", "a2a"], ["a2a", "responses"])
            or not isinstance(configuration, dict)
            or set(configuration) != set(protocols)
            or endpoint.get("authorization_schemes") != authorization
            or endpoint.get("version_selector") not in (selector, latest_selector)):
        raise RuntimeError("The existing Prompt does not match the reviewed A2A preconditions")
    desired_configuration = deepcopy(configuration)
    desired_configuration.setdefault("a2a", {})
    changes = {
        "agent_card": card,
        "agent_endpoint": {
            "protocol_configuration": desired_configuration,
        },
    }
    status = "verified-existing-a2a"
    if before.get("agent_card") != card or configuration != desired_configuration:
        client.agents.update_details(AGENT_NAME, body=changes)
        status = "enabled-a2a"
    after = client.agents.get(AGENT_NAME).as_dict()
    observed = after.get("agent_endpoint", {})
    verified_version = client.agents.get_version(AGENT_NAME, "1")
    if (after.get("name") != AGENT_NAME or after.get("state") != before["state"]
            or after.get("versions", {}).get("latest", {}).get("version") != "1"
            or verified_version.definition.as_dict() != definition.as_dict()
            or after.get("agent_card") != card
            or set(observed.get("protocols", [])) != {"responses", "a2a"}
            or observed.get("protocol_configuration") != desired_configuration
            or observed.get("version_selector") != endpoint["version_selector"]
            or observed.get("authorization_schemes") != authorization
            or after.get("instance_identity") != before.get("instance_identity")):
        raise RuntimeError("The independent A2A read-back differs from the reviewed configuration")
    return {
        "status": status, "agent_name": AGENT_NAME, "version": "1",
        "card_version": card["version"], "protocols": observed["protocols"],
        **definition_metadata(definition),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-deployment", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--enable-a2a", action="store_true")
    parser.add_argument("--project-endpoint")
    parser.add_argument("--tenant-id")
    args = parser.parse_args(argv)
    if args.apply:
        if (not args.project_endpoint or not re.fullmatch(
            r"https://[a-z0-9][a-z0-9-]*\.services\.ai\.azure\.com"
            r"/api/projects/[A-Za-z0-9][A-Za-z0-9_-]*", args.project_endpoint,
        )):
            parser.error("--apply requires an explicit canonical Foundry project endpoint")
        if not args.tenant_id:
            parser.error("--apply requires an explicit tenant UUID")
        try:
            tenant = str(UUID(args.tenant_id))
        except ValueError:
            parser.error("--tenant-id must be a UUID")
        config_dir = os.environ.get("AZURE_CONFIG_DIR")
        if not config_dir or not Path(config_dir).is_dir():
            parser.error("--apply requires the selected existing AZURE_CONFIG_DIR")
    try:
        definition = build_definition(args.model_deployment)
        if not args.apply:
            preview = {
                "status": "local-preview", "agent_name": AGENT_NAME,
                "definition": definition.as_dict(), "metadata": definition_metadata(definition),
            }
            if args.enable_a2a:
                preview["agent_card"] = json.loads((ROOT / CARD).read_text(encoding="utf-8"))
                preview["endpoint_change"] = {
                    "add_protocol": "a2a", "reviewed_agent_version": "1",
                    "preserve_responses_authorization_and_selector": True,
                }
            print(json.dumps(preview, indent=2))
            return 0
        with requests.Session() as session, AzureCliCredential(
            tenant_id=tenant, process_timeout=15,
        ) as credential:
            session.trust_env = False
            with AIProjectClient(
                endpoint=args.project_endpoint, credential=credential,
                transport=RequestsTransport(session=session, session_owner=False),
                connection_timeout=5, read_timeout=30, retry_total=0,
            ) as client:
                operation = enable_or_verify_a2a if args.enable_a2a else create_or_verify
                print(json.dumps(operation(client, definition), indent=2))
        return 0
    except (AzureError, OSError, RuntimeError, ValueError) as error:
        print(json.dumps({
            "error": "Prompt A2A configuration failed" if args.enable_a2a
            else "Prompt preparation or creation failed",
            "error_type": type(error).__name__,
            "http_status": getattr(error, "status_code", None),
        }), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
