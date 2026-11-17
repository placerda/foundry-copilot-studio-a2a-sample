"""Preview offline or create one unselected Motors Router image or Teams candidate.

Apply requires the selected existing AZURE_CONFIG_DIR and private project access.
Activation, business acceptance and Function retirement are never performed.
"""

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
from azure.core.exceptions import AzureError
from azure.core.pipeline.policies import RedirectPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential

from contoso.configuration import platform_callers
from contoso.router.core import CatalogError
from .create_image import ACR_API, ARM_SCOPE, TASK_NAME, source_archive
from .motors_admin import AdministrationFailure, Bindings, MotorsAdministrator, ROOT, ROUTER, digest


_HASH = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(
    r"(?P<registry>[a-z][a-z0-9]{4,49})\.azurecr\.io/contoso-runtime@"
    r"(?P<digest>sha256:[0-9a-f]{64})"
)
_TASK = re.compile(
    r"(?P<project_scope>/subscriptions/[0-9a-f-]{36}/"
    r"resourceGroups/[A-Za-z0-9_.()-]{1,90})/providers/"
    r"Microsoft.ContainerRegistry/registries/(?P<registry>[a-z][a-z0-9]{4,49})/"
    r"tasks/" + re.escape(TASK_NAME)
)
_EVIDENCE_FIELDS = {
    "task_id", "task_principal_id", "run_id", "status", "source_sha256", "image",
}
_TEAMS_CALLERS = "CONTOSO_TEAMS_PLATFORM_CALLER_IDS"
_LIMITS = {
    "activation_performed": False,
    "business_acceptance_performed": False,
    "function_retirement_performed": False,
    "limits": "Activation, business acceptance and Function retirement were NOT performed.",
}


class PublicationFailure(RuntimeError):
    pass


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate image evidence fields")
        result[key] = value
    return result


def review_build(bindings, evidence_path, expected_source_sha256, expected_image):
    """Validate reviewed local evidence and the actual archive without credentials."""
    if not isinstance(expected_source_sha256, str) or not _HASH.fullmatch(expected_source_sha256):
        raise ValueError("An explicit reviewed source SHA256 is required")
    image = _IMAGE.fullmatch(expected_image)
    if image is None:
        raise ValueError("An explicit contoso-runtime ACR digest pin is required")
    path = Path(evidence_path)
    if (any(part.is_symlink() or part.is_junction() for part in (path, *path.parents))
            or not path.is_file() or not 0 < path.stat().st_size <= 16384):
        raise ValueError("Invalid image evidence file")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("Image evidence exceeded its byte limit")
    evidence = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_fields)
    if (not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS
            or any(not isinstance(value, str) for value in evidence.values())):
        raise ValueError("Incomplete or unexpected image evidence fields")
    task = _TASK.fullmatch(evidence["task_id"])
    project_scope = bindings.project_resource_id.split("/providers/", 1)[0]
    if (task is None or task["project_scope"].casefold() != project_scope.casefold()
            or task["registry"] != image["registry"]):
        raise ValueError("The reviewed build must use the selected project's registry scope")
    if str(UUID(evidence["task_principal_id"])) != evidence["task_principal_id"]:
        raise ValueError("A canonical build task principal UUID is required")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", evidence["run_id"]):
        raise ValueError("Invalid reviewed build run ID")
    if (evidence["status"] != "Succeeded"
            or evidence["source_sha256"] != expected_source_sha256
            or evidence["image"] != expected_image):
        raise ValueError("The build evidence differs from the explicit reviewed pins")
    if hashlib.sha256(source_archive(ROOT)).hexdigest() != expected_source_sha256:
        raise ValueError("The current source archive differs from the reviewed source pin")
    return evidence


def verify_build(administrator, evidence):
    """Read only fixed ARM resources, never a URL supplied by build evidence."""
    registry_id = evidence["task_id"].rsplit("/tasks/", 1)[0]
    run_id = registry_id + "/runs/" + evidence["run_id"]
    registry = administrator._get_json(
        f"https://management.azure.com{registry_id}?api-version={ACR_API}", ARM_SCOPE,
    )
    login_server, expected_digest = evidence["image"].split("/contoso-runtime@")
    properties = registry.get("properties", {})
    if (registry.get("id", "").casefold() != registry_id.casefold()
            or properties.get("publicNetworkAccess") != "Disabled"
            or properties.get("loginServer") != login_server):
        raise PublicationFailure("private_build_registry_mismatch")
    run = administrator._get_json(
        f"https://management.azure.com{run_id}?api-version={ACR_API}", ARM_SCOPE,
    )
    properties = run.get("properties", {})
    images = properties.get("outputImages", [])
    if (run.get("id", "").casefold() != run_id.casefold()
            or properties.get("runId") != evidence["run_id"]
            or properties.get("task") != TASK_NAME
            or properties.get("status") != "Succeeded"
            or not isinstance(images, list)):
        raise PublicationFailure("build_run_mismatch")
    matching = [
        item for item in images if isinstance(item, dict)
        and item.get("repository") == "contoso-runtime"
        and item.get("tag") == evidence["source_sha256"]
    ]
    if (len(matching) != 1 or matching[0].get("digest") != expected_digest
            or matching[0].get("registry", login_server) != login_server):
        raise PublicationFailure("build_output_image_mismatch")


def _active_version(administrator):
    bindings = administrator.bindings
    active = administrator.client.agents.get_version(ROUTER, bindings.router_version)
    if (active.name != ROUTER or active.version != bindings.router_version
            or digest(active.definition.as_dict()) != bindings.router_definition_sha256):
        raise PublicationFailure("active_router_definition_drift")
    return active


def _parent_invariants(parent):
    # Version creation may update latest bookkeeping, not the live endpoint or registration.
    return {
        key: value for key, value in parent.items()
        if key not in ("versions", "updated_at", "etag")
    }


def _verify_unchanged(administrator, before, definition):
    after = administrator.client.agents.get(ROUTER).as_dict()
    if _parent_invariants(after) != _parent_invariants(before):
        raise PublicationFailure("active_router_endpoint_or_parent_drift")
    if _active_version(administrator).definition.as_dict() != definition:
        raise PublicationFailure("active_router_definition_drift")


def _create_candidate(administrator, before, definition, desired, metadata, result):
    expected = desired.as_dict()
    client, bindings = administrator.client, administrator.bindings
    try:
        created = client.agents.create_version(ROUTER, definition=desired, metadata=metadata)
        if (created.name != ROUTER or not isinstance(created.version, str)
                or not re.fullmatch(r"[1-9][0-9]{0,9}", created.version)
                or created.version == bindings.router_version):
            raise PublicationFailure("invalid_candidate_version_response")
        observed = client.agents.get_version(ROUTER, created.version)
        if (observed.name != ROUTER or observed.version != created.version
                or observed.definition.as_dict() != expected or observed.metadata != metadata):
            raise PublicationFailure("candidate_definition_or_metadata_readback_mismatch")
        _verify_unchanged(administrator, before, definition)
    except (AzureError, requests.RequestException) as error:
        raise PublicationFailure(
            "candidate_creation_or_verification_failed_outcome_unconfirmed",
        ) from error
    return {
        **result, "status": "created-verified-candidate", "version": created.version,
        "candidate_created": True, "definition_sha256": digest(expected),
        "active_state_unchanged": True,
    }


def publish_candidate(administrator, evidence):
    """One explicit apply: verified no-op or one candidate, with no activation/retry."""
    administrator.run("preflight")
    verify_build(administrator, evidence)
    client, bindings = administrator.client, administrator.bindings
    before = deepcopy(client.agents.get(ROUTER).as_dict())
    # Fence the captured baseline against the reviewed live pins before any mutation.
    administrator.run("preflight")
    active = _active_version(administrator)
    definition = deepcopy(active.definition.as_dict())
    configuration = definition.get("container_configuration")
    if (definition.get("kind") != "hosted" or not isinstance(configuration, dict)
            or not isinstance(configuration.get("image"), str)):
        raise PublicationFailure("image_based_hosted_router_required")
    result = {
        "agent_name": ROUTER, "base_version": bindings.router_version,
        "source_sha256": evidence["source_sha256"], "image": evidence["image"],
        **_LIMITS,
    }
    if configuration["image"] == evidence["image"]:
        _verify_unchanged(administrator, before, definition)
        return {
            **result, "status": "verified-already-current",
            "version": bindings.router_version, "candidate_created": False,
            "active_state_unchanged": True,
        }
    desired = deepcopy(active.definition)
    desired.container_configuration.image = evidence["image"]
    expected = deepcopy(definition)
    expected["container_configuration"]["image"] = evidence["image"]
    if desired.as_dict() != expected:
        raise PublicationFailure("candidate_clone_mismatch")
    metadata = {
        "contoso-source-sha256": evidence["source_sha256"],
        "contoso-base-version": bindings.router_version,
        "contoso-purpose": "motors-router-image-candidate",
    }
    return _create_candidate(administrator, before, definition, desired, metadata, result)


def _review_teams_callers(callers):
    if not isinstance(callers, (list, tuple)):
        raise ValueError("An explicit list of observed Teams platform callers is required")
    try:
        checked = platform_callers({_TEAMS_CALLERS: json.dumps(callers)}, setting=_TEAMS_CALLERS)
    except CatalogError:
        raise ValueError("Invalid reviewed Teams platform callers") from None
    if any(caller == "*" or caller.startswith("unbound:") for caller in checked):
        raise ValueError("Observed Teams platform callers must replace unbound placeholders")
    return sorted(checked)


def publish_teams_candidate(administrator, callers):
    """Change only Teams admission, never Python admission, image or active state."""
    callers = _review_teams_callers(callers)
    administrator.run("preflight")
    client, bindings = administrator.client, administrator.bindings
    before = deepcopy(client.agents.get(ROUTER).as_dict())
    administrator.run("preflight")
    active = _active_version(administrator)
    definition = deepcopy(active.definition.as_dict())
    env = definition.get("environment_variables")
    protocols = definition.get("protocol_versions")
    if (definition.get("kind") != "hosted" or not isinstance(env, dict)
            or env.get("CONTOSO_ENTRA_TENANT_ID") != bindings.tenant_id
            or not isinstance(protocols, list)
            or not any(isinstance(item, dict) and item.get("protocol") == "activity"
                       for item in protocols)):
        raise PublicationFailure("existing_native_teams_router_required")
    try:
        current = platform_callers(env, setting=_TEAMS_CALLERS)
    except CatalogError:
        raise PublicationFailure("existing_teams_admission_required") from None
    result = {
        "agent_name": ROUTER, "base_version": bindings.router_version,
        "source_sha256": bindings.router_source_sha256,
        "teams_caller_count": len(callers), **_LIMITS,
    }
    if current == frozenset(callers):
        _verify_unchanged(administrator, before, definition)
        return {
            **result, "status": "verified-already-current", "version": bindings.router_version,
            "candidate_created": False, "active_state_unchanged": True,
        }
    desired = deepcopy(active.definition)
    desired.environment_variables[_TEAMS_CALLERS] = json.dumps(callers, separators=(",", ":"))
    expected = deepcopy(definition)
    expected["environment_variables"][_TEAMS_CALLERS] = json.dumps(callers, separators=(",", ":"))
    if desired.as_dict() != expected:
        raise PublicationFailure("candidate_clone_mismatch")
    return _create_candidate(administrator, before, definition, desired, {
        "contoso-source-sha256": bindings.router_source_sha256,
        "contoso-base-version": bindings.router_version,
        "contoso-purpose": "motors-router-teams-admission-candidate",
    }, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bindings", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image-evidence", type=Path)
    mode.add_argument("--teams-platform-caller", action="append")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-image")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.image_evidence and not (args.expected_source_sha256 and args.expected_image):
        parser.error("Image publication requires explicit source and image pins")
    if args.teams_platform_caller is not None and (args.expected_source_sha256 or args.expected_image):
        parser.error("Teams admission publication cannot change image pins")
    config_dir = os.environ.get("AZURE_CONFIG_DIR")
    if args.apply and (not config_dir or not Path(config_dir).is_dir()):
        parser.error("--apply requires the selected existing AZURE_CONFIG_DIR")
    try:
        bindings = Bindings.load(args.bindings)
        if args.image_evidence:
            evidence = review_build(
                bindings, args.image_evidence, args.expected_source_sha256, args.expected_image,
            )
            preview = {"source_sha256": evidence["source_sha256"], "image": evidence["image"]}
        else:
            callers = _review_teams_callers(args.teams_platform_caller)
            preview = {
                "source_sha256": bindings.router_source_sha256, "teams_caller_count": len(callers),
            }
        if not args.apply:
            print(json.dumps({
                "status": "offline-preview", "agent_name": ROUTER,
                "base_version": bindings.router_version,
                **preview,
                "live_state_verified": False, "candidate_created": False, **_LIMITS,
            }, indent=2))
            return 0
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
                administrator = MotorsAdministrator(bindings, credential, session, client)
                result = (publish_candidate(administrator, evidence) if args.image_evidence
                          else publish_teams_candidate(administrator, callers))
        print(json.dumps(result, indent=2))
        return 0
    except (PublicationFailure, AdministrationFailure) as error:
        code = str(error)
    except (AzureError, requests.RequestException):
        code = "publication_dependency_failed_outcome_unconfirmed"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        code = "invalid_publication_configuration_or_response"
    print(json.dumps({
        "status": "failed", "error": code, **_LIMITS,
        "automatic_retry_performed": False, "automatic_restore_performed": False,
        "independent_review_required_before_retry": bool(args.apply),
    }), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
