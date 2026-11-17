"""Read-only Motors administration from an explicitly authorized private workstation."""

import argparse
from dataclasses import dataclass, fields
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
from uuid import UUID

import requests
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.core.pipeline.policies import RedirectPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential

from .create_image import source_archive
from .create_prompt import build_definition


ROOT = Path(__file__).resolve().parents[2]
ROUTER = "contoso-foundry-agent"
LOGISTICS = "contoso-motors-logistics"
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_PROJECT_ID = re.compile(
    r"/subscriptions/(?P<subscription>[0-9a-f-]{36})/"
    r"resourceGroups/[A-Za-z0-9_.()-]{1,90}/providers/"
    r"Microsoft.CognitiveServices/accounts/(?P<account>[a-z0-9][a-z0-9-]{0,62})/"
    r"projects/(?P<project>[A-Za-z0-9][A-Za-z0-9_-]{0,127})"
)


class AdministrationFailure(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate administration binding")
        result[key] = value
    return result


@dataclass(frozen=True)
class Bindings:
    schema_version: int
    tenant_id: str
    operator_object_id: str
    project_resource_id: str
    project_endpoint: str
    router_version: str
    router_definition_sha256: str
    router_source_sha256: str
    router_principal_id: str
    logistics_version: str
    logistics_definition_sha256: str
    logistics_principal_id: str
    logistics_model: str
    studio_source_sha256: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported administration binding schema")
        if any(not isinstance(getattr(self, field.name), str)
               for field in fields(self) if field.name != "schema_version"):
            raise ValueError("Administration bindings must be explicit strings")
        for value in (
            self.tenant_id, self.operator_object_id,
            self.router_principal_id, self.logistics_principal_id,
        ):
            if str(UUID(value)) != value:
                raise ValueError("Canonical identity UUIDs are required")
        match = _PROJECT_ID.fullmatch(self.project_resource_id)
        if match is None or str(UUID(match["subscription"])) != match["subscription"]:
            raise ValueError("An explicit canonical Foundry project resource ID is required")
        if self.project_endpoint != (
            f"https://{match['account']}.services.ai.azure.com/api/projects/{match['project']}"
        ):
            raise ValueError("The project endpoint and resource ID must identify the same project")
        for value in (self.router_version, self.logistics_version):
            if not re.fullmatch(r"[1-9][0-9]{0,9}", value):
                raise ValueError("Explicit immutable numeric versions are required")
        for value in (
            self.router_definition_sha256, self.router_source_sha256,
            self.logistics_definition_sha256, self.studio_source_sha256,
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Reviewed SHA256 pins are required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.logistics_model):
            raise ValueError("An explicit logistics model deployment is required")
        if self.router_principal_id == self.logistics_principal_id:
            raise ValueError("The two agents must retain separate identities")

    @classmethod
    def load(cls, path):
        if path.is_symlink() or not 0 < path.stat().st_size <= 16384:
            raise ValueError("Invalid administration binding file")
        with path.open(encoding="utf-8-sig") as stream:
            value = json.load(stream, object_pairs_hook=_unique_fields)
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("Incomplete or unexpected administration binding fields")
        return cls(**value)


class MotorsAdministrator:
    def __init__(self, bindings, credential, session, client):
        self.bindings = bindings
        self.credential = credential
        self.session = session
        self.client = client

    def _get_json(self, url, scope):
        token = self.credential.get_token(scope)
        with self.session.get(
            url, headers={"Authorization": "Bearer " + token.token},
            timeout=(5, 30), allow_redirects=False, stream=True,
        ) as response:
            if response.status_code != 200 or response.history:
                raise AdministrationFailure("administration_read_failed")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk)
                if len(body) > 262144:
                    raise AdministrationFailure("administration_response_too_large")
        value = json.loads(body, object_pairs_hook=_unique_fields)
        if not isinstance(value, dict):
            raise AdministrationFailure("invalid_administration_response")
        return value

    def _private_project(self):
        cfg = self.bindings
        identity = self._get_json(
            "https://graph.microsoft.com/v1.0/me?$select=id",
            "https://graph.microsoft.com/.default",
        )
        if identity.get("id") != cfg.operator_object_id:
            raise AdministrationFailure("selected_operator_mismatch")
        parent = cfg.project_resource_id.rsplit("/projects/", 1)[0]
        for resource_id in (parent, cfg.project_resource_id):
            record = self._get_json(
                f"https://management.azure.com{resource_id}?api-version=2025-06-01",
                "https://management.azure.com/.default",
            )
            if record.get("id", "").casefold() != resource_id.casefold():
                raise AdministrationFailure("selected_resource_mismatch")
            if resource_id == parent and (
                record.get("properties", {}).get("publicNetworkAccess") != "Disabled"
            ):
                raise AdministrationFailure("private_foundry_required")
        host = cfg.project_endpoint.split("/")[2]
        addresses = {
            entry[4][0] for entry in socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
        }
        if not addresses or any(
            not any(ipaddress.ip_address(address) in network for network in _PRIVATE_NETWORKS)
            for address in addresses
        ):
            raise AdministrationFailure("private_project_dns_required")
        return sorted(addresses)

    def _records(self):
        cfg = self.bindings
        records, definitions = [], {}
        for name, version, expected_hash, principal, protocols in (
            (ROUTER, cfg.router_version, cfg.router_definition_sha256,
             cfg.router_principal_id, {"a2a", "activity", "invocations", "responses"}),
            (LOGISTICS, cfg.logistics_version, cfg.logistics_definition_sha256,
             cfg.logistics_principal_id, {"a2a", "responses"}),
        ):
            agent = self.client.agents.get(name).as_dict()
            endpoint = agent.get("agent_endpoint", {})
            selected = {"version_selection_rules": [
                {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100},
            ]}
            if (agent.get("name") != name or agent.get("state") != "enabled"
                    or endpoint.get("version_selector") != selected
                    or set(endpoint.get("protocols", [])) != protocols
                    or agent.get("instance_identity", {}).get("principal_id") != principal):
                raise AdministrationFailure("agent_binding_drift")
            schemes = endpoint.get("authorization_schemes", [])
            expected_schemes = [{"type": "Entra", "isolation_key_source": {"kind": "Entra"}}]
            if name == ROUTER:
                expected_schemes.append({"type": "BotServiceRbac"})
            if schemes != expected_schemes:
                raise AdministrationFailure("agent_authorization_drift")
            actual = self.client.agents.get_version(name, version)
            definition = actual.definition.as_dict()
            if actual.name != name or actual.version != version or digest(definition) != expected_hash:
                raise AdministrationFailure("agent_definition_drift")
            definitions[name] = definition
            records.append({
                "name": name, "version": version, "definition_sha256": expected_hash,
                "principal_id": principal, "protocols": sorted(protocols),
                "version_selector": selected,
            })
        return records, definitions

    def run(self, operation, *, agent=None, session_id=None, root=ROOT):
        addresses = self._private_project()
        records, definitions = self._records()
        result = {
            "status": "verified-read-only-operation", "operation": operation,
            "read_only": True, "function_called": False,
            "business_acceptance_proven": False, "function_retirement_ready": False,
            "records": records,
        }
        if operation in ("preflight", "diagnostics"):
            result["private_project_addresses"] = addresses
        elif operation == "sources":
            source = root / "contoso-demo" / "knowledge" / "motors-supplier-delivery-terms-v1.md"
            studio_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            source_hash = hashlib.sha256(source_archive(root)).hexdigest()
            if studio_hash != self.bindings.studio_source_sha256:
                raise AdministrationFailure("local_studio_source_drift")
            if source_hash != self.bindings.router_source_sha256:
                raise AdministrationFailure("local_router_source_drift")
            if definitions[LOGISTICS] != build_definition(
                self.bindings.logistics_model, root=root,
            ).as_dict():
                raise AdministrationFailure("embedded_logistics_source_drift")
            result["sources"] = {
                "router_source_sha256": source_hash,
                "local_studio_source_sha256": studio_hash,
                "logistics_embedded_reference_verified": True,
                "remote_studio_source_checked": False,
                "sources_published": False,
            }
        elif operation == "a2a-session-snapshot":
            if (agent not in (ROUTER, LOGISTICS) or not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", session_id)):
                raise ValueError("An explicit agent and opaque session ID are required")
            session = self.client.agents.get_session(agent, session_id)
            version = (
                self.bindings.router_version if agent == ROUTER else self.bindings.logistics_version
            )
            if (session.agent_session_id != session_id or session.version_indicator.as_dict() != {
                "type": "version_ref", "agent_version": version,
            }):
                raise AdministrationFailure("session_version_mismatch")
            result["session"] = {
                "agent_name": agent, "agent_session_id": session_id, "version": version,
                "status": session.status.value, "created_at": session.created_at.isoformat(),
            }
        elif operation not in ("records", "metadata"):
            raise ValueError("Unsupported read-only administration operation")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("operation", choices=(
        "preflight", "records", "metadata", "sources", "diagnostics", "a2a-session-snapshot",
    ))
    parser.add_argument("--agent", choices=(ROUTER, LOGISTICS))
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    config_dir = os.environ.get("AZURE_CONFIG_DIR")
    if not config_dir or not Path(config_dir).is_dir():
        parser.error("An explicit existing AZURE_CONFIG_DIR is required")
    if args.operation == "a2a-session-snapshot" and (not args.agent or not args.session_id):
        parser.error("A session snapshot requires --agent and --session-id")
    if args.operation != "a2a-session-snapshot" and (args.agent or args.session_id):
        parser.error("Session arguments are only valid for a session snapshot")
    try:
        bindings = Bindings.load(args.bindings)
        with AzureCliCredential(
            tenant_id=bindings.tenant_id, process_timeout=20,
        ) as credential, requests.Session() as session:
            session.trust_env = False
            with AIProjectClient(
                endpoint=bindings.project_endpoint, credential=credential, retry_total=0,
                redirect_policy=RedirectPolicy(redirect_max=0),
                transport=RequestsTransport(session=session, session_owner=False),
                connection_timeout=5, read_timeout=30,
            ) as client:
                result = MotorsAdministrator(bindings, credential, session, client).run(
                    args.operation, agent=args.agent, session_id=args.session_id,
                )
        print(json.dumps(result, indent=2))
        return 0
    except AdministrationFailure as error:
        code = str(error)
    except (AzureError, requests.RequestException) as error:
        code = "administration_dependency_unavailable"
    except (OSError, ValueError):
        code = "invalid_administration_configuration_or_response"
    logging.getLogger(__name__).error("Motors administration failed: %s", code)
    print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
