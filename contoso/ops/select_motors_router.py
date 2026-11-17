"""Preview or explicitly select a reviewed Motors Router version without the Function.

The default is a live read-only preview. A changed selection requires reviewed
parent and definition pins and an exclusive operator change window.
"""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys

import requests
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.core.pipeline.policies import RedirectPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential

from .motors_admin import AdministrationFailure, Bindings, MotorsAdministrator, ROUTER, digest
from .publish_motors_router import _parent_invariants


_HASH = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[1-9][0-9]{0,9}")
_LIMITS = {
    "function_called": False,
    "business_acceptance_proven": False,
    "function_retirement_ready": False,
    "automatic_retry_performed": False,
    "automatic_restore_performed": False,
    "service_compare_and_swap_proven": False,
}


class SelectionFailure(RuntimeError):
    def __init__(self, code, *, mutation_attempted=False):
        super().__init__(code)
        self.mutation_attempted = mutation_attempted


def _selector(version):
    return {"version_selection_rules": [
        {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100},
    ]}


def _definition(client, version, expected_hash):
    record = client.agents.get_version(ROUTER, version)
    definition = record.definition.as_dict()
    if (record.name != ROUTER or record.version != version
            or definition.get("kind") != "hosted" or digest(definition) != expected_hash):
        raise SelectionFailure("reviewed_router_version_mismatch")
    return definition


def _parent(client, expected_hash=None):
    parent = deepcopy(client.agents.get(ROUTER).as_dict())
    parent_hash = digest(_parent_invariants(parent))
    if expected_hash is not None and parent_hash != expected_hash:
        raise SelectionFailure("reviewed_router_parent_drift")
    return parent, parent_hash


def select_version(
    administrator, target_version, target_definition_sha256, *,
    expected_parent_sha256=None, apply=False, exclusive_change_window=False,
):
    """Patch only the selector, with explicit rollback pins and no automatic retry."""
    if (not isinstance(target_version, str) or not _VERSION.fullmatch(target_version)
            or not isinstance(target_definition_sha256, str)
            or not _HASH.fullmatch(target_definition_sha256)):
        raise ValueError("An explicit numeric version and reviewed definition SHA256 are required")
    if expected_parent_sha256 is not None and (
        not isinstance(expected_parent_sha256, str) or not _HASH.fullmatch(expected_parent_sha256)
    ):
        raise ValueError("Invalid reviewed parent SHA256")
    if apply and expected_parent_sha256 is None:
        raise ValueError("Apply requires the reviewed preview's parent SHA256")
    bindings, client = administrator.bindings, administrator.client
    changing = target_version != bindings.router_version
    if apply and changing and not exclusive_change_window:
        raise ValueError("A changed selection requires an exclusive operator change window")

    administrator.run("preflight")
    before, before_hash = _parent(client, expected_parent_sha256)
    if before.get("agent_endpoint", {}).get("version_selector") != _selector(
        bindings.router_version,
    ):
        raise SelectionFailure("reviewed_router_selector_drift")
    active = _definition(client, bindings.router_version, bindings.router_definition_sha256)
    target = _definition(client, target_version, target_definition_sha256)
    if (not isinstance(active.get("protocol_versions"), list)
            or not active["protocol_versions"]
            or target.get("protocol_versions") != active["protocol_versions"]
            or not isinstance(active.get("environment_variables"), dict)
            or not isinstance(target.get("environment_variables"), dict)):
        raise SelectionFailure("router_protocol_or_environment_contract_drift")
    for key in ("CONTOSO_ENTRA_TENANT_ID", "CONTOSO_PLATFORM_CALLER_IDS"):
        if (not isinstance(active["environment_variables"].get(key), str)
                or not active["environment_variables"][key]
                or target["environment_variables"].get(key) != active["environment_variables"][key]):
            raise SelectionFailure("router_python_admission_drift")

    after = deepcopy(before)
    after["agent_endpoint"]["version_selector"] = _selector(target_version)
    after_hash = digest(_parent_invariants(after))
    target_bindings = replace(
        bindings, router_version=target_version,
        router_definition_sha256=target_definition_sha256,
    )
    rollback = {
        "from_version": target_version,
        "from_definition_sha256": target_definition_sha256,
        "target_version": bindings.router_version,
        "target_definition_sha256": bindings.router_definition_sha256,
        "expected_parent_sha256": after_hash,
        "requires_updated_from_bindings": changing,
        "requires_exclusive_change_window": changing,
    }
    result = {
        **_LIMITS,
        "agent_name": ROUTER,
        "from_version": bindings.router_version,
        "target_version": target_version,
        "target_definition_sha256": target_definition_sha256,
        "expected_parent_sha256": before_hash,
        "selected_parent_sha256": after_hash,
        "rollback": rollback,
        "selector_mutation_performed": False,
        "live_state_verified": True,
    }
    # This fences observed drift, not an atomic service-side conditional update.
    administrator.run("preflight")
    _parent(client, before_hash)
    _definition(client, target_version, target_definition_sha256)
    if not changing:
        return {**result, "status": "verified-already-selected"}
    if not apply:
        return {**result, "status": "verified-selection-preview"}

    try:
        client.agents.update_details(ROUTER, body={
            "agent_endpoint": {"version_selector": _selector(target_version)},
        })
        _parent(client, after_hash)
        _definition(client, target_version, target_definition_sha256)
        MotorsAdministrator(
            target_bindings, administrator.credential, administrator.session, client,
        ).run("preflight")
    except (
        AzureError, requests.RequestException, AdministrationFailure, SelectionFailure,
        ValueError, TypeError, KeyError, AttributeError,
    ) as error:
        raise SelectionFailure(
            "selector_write_or_readback_failed_outcome_unconfirmed",
            mutation_attempted=True,
        ) from error
    return {**result, "status": "selected-and-verified", "selector_mutation_performed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--target-definition-sha256", required=True)
    parser.add_argument("--expected-parent-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--exclusive-change-window", action="store_true")
    args = parser.parse_args(argv)
    config_dir = os.environ.get("AZURE_CONFIG_DIR")
    if not config_dir or not Path(config_dir).is_dir():
        parser.error("An explicit existing AZURE_CONFIG_DIR is required")
    if args.apply and not args.expected_parent_sha256:
        parser.error("--apply requires the reviewed preview's --expected-parent-sha256")
    mutation_attempted = False
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
                result = select_version(
                    MotorsAdministrator(bindings, credential, session, client),
                    args.target_version, args.target_definition_sha256,
                    expected_parent_sha256=args.expected_parent_sha256,
                    apply=args.apply, exclusive_change_window=args.exclusive_change_window,
                )
                mutation_attempted = result["selector_mutation_performed"]
        print(json.dumps(result, indent=2))
        return 0
    except SelectionFailure as error:
        code, mutation_attempted = str(error), error.mutation_attempted
    except AdministrationFailure as error:
        code = str(error)
    except (AzureError, requests.RequestException):
        code = "selection_dependency_unavailable"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        code = "invalid_selection_configuration_or_response"
    print(json.dumps({
        **_LIMITS, "status": "failed", "error": code,
        "selector_mutation_attempted": mutation_attempted,
        "independent_review_required_before_retry": mutation_attempted,
    }), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
