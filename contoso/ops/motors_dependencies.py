"""Verify the Motors Studio source and managed connection without the Function."""

import argparse
from dataclasses import dataclass, fields
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from uuid import UUID

from azure.core.exceptions import AzureError
from azure.core.pipeline.policies import RedirectPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import AzureCliCredential
from azure.ai.projects import AIProjectClient
import requests

from . import motors_admin as admin


@dataclass(frozen=True)
class DependencyBindings:
    schema_version: int
    dataverse_origin: str
    studio_bot_id: str
    studio_schema_name: str
    studio_source_component_id: str
    studio_instructions_sha256: str
    studio_published_on: str
    apim_resource_id: str
    apim_principal_id: str
    provider_name: str
    connection_name: str
    oauth_client_id: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported dependency binding schema")
        if any(not isinstance(getattr(self, field.name), str)
               for field in fields(self) if field.name != "schema_version"):
            raise ValueError("Explicit dependency strings are required")
        if not re.fullmatch(
            r"https://[a-z0-9][a-z0-9-]{0,62}\.api\.crm[0-9]*\.dynamics\.com",
            self.dataverse_origin,
        ):
            raise ValueError("A canonical Dataverse API origin is required")
        for value in (self.studio_bot_id, self.studio_source_component_id,
                      self.apim_principal_id, self.oauth_client_id):
            if str(UUID(value)) != value:
                raise ValueError("Canonical dependency UUIDs are required")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", self.studio_schema_name):
            raise ValueError("An explicit Studio schema name is required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.studio_instructions_sha256):
            raise ValueError("Reviewed Studio instructions SHA256 is required")
        _timestamp(self.studio_published_on)
        if not re.fullmatch(
            r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.()-]{1,90}/"
            r"providers/Microsoft.ApiManagement/service/[a-z][a-z0-9-]{0,49}",
            self.apim_resource_id,
        ):
            raise ValueError("An explicit APIM resource ID is required")
        for value in (self.provider_name, self.connection_name):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value):
                raise ValueError("Explicit managed connection names are required")

    @classmethod
    def load(cls, path):
        if path.is_symlink() or not 0 < path.stat().st_size <= 16384:
            raise ValueError("Invalid dependency binding file")
        value = json.loads(path.read_text(encoding="utf-8-sig"),
                           object_pairs_hook=admin._unique_fields)
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("Incomplete or unexpected dependency binding fields")
        return cls(**value)


def _timestamp(value):
    observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("A timezone-aware publication timestamp is required")
    return observed


class MotorsDependencies:
    def __init__(self, administrator, bindings):
        self.administrator = administrator
        self.bindings = bindings
        project_group = administrator.bindings.project_resource_id.split("/providers/", 1)[0]
        if not bindings.apim_resource_id.startswith(
            project_group + "/providers/Microsoft.ApiManagement/service/",
        ):
            raise ValueError("APIM must belong to the selected subscription and resource group")

    def _arm(self, resource_id):
        value = self.administrator._get_json(
            "https://management.azure.com" + resource_id + "?api-version=2024-05-01",
            "https://management.azure.com/.default",
        )
        if value.get("id", "").casefold() != resource_id.casefold():
            raise admin.AdministrationFailure("dependency_resource_mismatch")
        return value

    def studio_source(self, *, root=admin.ROOT):
        cfg = self.bindings
        local = (
            root / "contoso-demo" / "knowledge" / "motors-supplier-delivery-terms-v1.md"
        ).read_bytes()
        if hashlib.sha256(local).hexdigest() != self.administrator.bindings.studio_source_sha256:
            raise admin.AdministrationFailure("local_studio_source_drift")
        base = cfg.dataverse_origin + "/api/data/v9.2/"
        scope = cfg.dataverse_origin.replace(".api.crm", ".crm") + "/.default"
        bot = self.administrator._get_json(
            base + f"bots({cfg.studio_bot_id})"
            "?$select=botid,name,schemaname,configuration,publishedon", scope,
        )
        if (bot.get("botid") != cfg.studio_bot_id
                or bot.get("name") != "Contoso Motors Supplier"
                or bot.get("schemaname") != cfg.studio_schema_name
                or _timestamp(bot["publishedon"]) != _timestamp(cfg.studio_published_on)):
            raise admin.AdministrationFailure("studio_binding_drift")
        configuration = json.loads(bot["configuration"], object_pairs_hook=admin._unique_fields)
        segments = configuration["agentSettings"]["instructions"]["segments"]
        if (len(segments) != 1 or segments[0].get("$kind") != "StaticSegment"
                or not isinstance(segments[0].get("value"), str)):
            raise admin.AdministrationFailure("studio_instructions_drift")
        instructions = segments[0]["value"]
        if hashlib.sha256(instructions.encode("utf-8")).hexdigest() != cfg.studio_instructions_sha256:
            raise admin.AdministrationFailure("studio_instructions_drift")
        prompt = (
            root / "contoso-demo" / "prompts" / "motors-supplier-v1.md"
        ).read_text(encoding="utf-8").strip()
        expected = (
            prompt + "\n\n<approved-source-reference>\n"
            + local.decode("utf-8") + "\n</approved-source-reference>"
        )
        if instructions != expected:
            raise admin.AdministrationFailure("studio_inline_reference_drift")
        component = self.administrator._get_json(
            base + f"botcomponents({cfg.studio_source_component_id})"
            "?$select=botcomponentid,schemaname,componenttype,_parentbotid_value", scope,
        )
        if (component.get("botcomponentid") != cfg.studio_source_component_id
                or component.get("_parentbotid_value") != cfg.studio_bot_id
                or component.get("componenttype") != 14
                or component.get("schemaname") != (
                    cfg.studio_schema_name + ".file.motorssupplierdeliverytermsv1"
                )):
            raise admin.AdministrationFailure("studio_source_binding_drift")
        token = self.administrator.credential.get_token(scope)
        with self.administrator.session.get(
            base + f"botcomponents({cfg.studio_source_component_id})/filedata/$value",
            headers={"Authorization": "Bearer " + token.token},
            timeout=(5, 30), allow_redirects=False, stream=True,
        ) as response:
            if response.status_code != 200 or response.history:
                raise admin.AdministrationFailure("studio_source_read_failed")
            source = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                source.extend(chunk)
                if len(source) > 32768:
                    raise admin.AdministrationFailure("studio_source_too_large")
        if source != local:
            raise admin.AdministrationFailure("remote_studio_source_drift")
        return {
            "remote_studio_source_checked": True, "attached_source_matches_local_bytes": True,
            "inline_source_matches_local_bytes": True, "published_on": bot["publishedon"],
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "instructions_sha256": cfg.studio_instructions_sha256, "sources_published": False,
            "draft_instructions_match_reviewed_publication": True,
            "runtime_source_retrieval_proven": False,
        }

    def connection(self):
        cfg = self.bindings
        tenant = self.administrator.bindings.tenant_id
        service = self._arm(cfg.apim_resource_id)
        if service.get("identity", {}).get("principalId") != cfg.apim_principal_id:
            raise admin.AdministrationFailure("apim_identity_drift")
        provider_id = cfg.apim_resource_id + "/authorizationProviders/" + cfg.provider_name
        provider = self._arm(provider_id)["properties"]
        oauth = provider["oauth2"]
        grant = oauth["grantTypes"]["authorizationCode"]
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        apim_name = cfg.apim_resource_id.rsplit("/", 1)[1]
        scopes = grant.get("scopes", "").split()
        if (provider.get("identityProvider") != "oauth2pkce"
                or oauth.get("redirectUrl") != (
                    "https://authorization-manager.consent.azure-apim.net/redirect/apim/" + apim_name
                )
                or grant.get("clientId") != cfg.oauth_client_id
                or grant.get("authorizationUrl") != (
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
                )
                or grant.get("tokenUrl") != token_url or grant.get("refreshUrl") != token_url
                or sorted(scopes) != sorted([
                    "https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A",
                    "offline_access",
                ])):
            raise admin.AdministrationFailure("managed_connection_provider_drift")
        connection_id = provider_id + "/authorizations/" + cfg.connection_name
        connection = self._arm(connection_id)["properties"]
        if (connection.get("authorizationType") != "OAuth2"
                or connection.get("oauth2grantType") != "AuthorizationCode"
                or connection.get("status") != "Connected" or connection.get("error")):
            raise admin.AdministrationFailure("managed_connection_not_ready")
        policies = self.administrator._get_json(
            "https://management.azure.com" + connection_id
            + "/accessPolicies?api-version=2024-05-01",
            "https://management.azure.com/.default",
        )
        if policies.get("nextLink") or not isinstance(policies.get("value"), list):
            raise admin.AdministrationFailure("incomplete_connection_access_policies")
        expected = {"objectId": cfg.apim_principal_id, "tenantId": tenant}
        if not any(
            policy.get("id", "").startswith(connection_id + "/accessPolicies/")
            and all(policy.get("properties", {}).get(key) == value
                    for key, value in expected.items())
            for policy in policies["value"]
        ):
            raise admin.AdministrationFailure("apim_connection_access_missing")
        return {
            "managed_connection_status": "Connected", "pkce_configuration_verified": True,
            "apim_connection_access_verified": True, "managed_callback_configuration_verified": True,
            "legacy_function_callback_used": False, "studio_delegated_token_requested": False,
            "interactive_oauth_started": False, "refresh_proven": False,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("operation", choices=("studio-source", "connection"))
    args = parser.parse_args(argv)
    config_dir = os.environ.get("AZURE_CONFIG_DIR")
    if not config_dir or not Path(config_dir).is_dir():
        parser.error("An explicit existing AZURE_CONFIG_DIR is required")
    try:
        cfg = admin.Bindings.load(args.bindings)
        dependencies = DependencyBindings.load(args.dependencies)
        with (
            AzureCliCredential(tenant_id=cfg.tenant_id, process_timeout=20) as credential,
            requests.Session() as session,
        ):
            session.trust_env = False
            transport = RequestsTransport(session=session, session_owner=False)
            with AIProjectClient(
                endpoint=cfg.project_endpoint, credential=credential, transport=transport,
                retry_total=0, redirect_policy=RedirectPolicy(redirect_max=0),
                connection_timeout=5, read_timeout=30,
            ) as client:
                administrator = admin.MotorsAdministrator(cfg, credential, session, client)
                verifier = MotorsDependencies(administrator, dependencies)
                administrator.run("preflight")
                result = (
                    verifier.studio_source() if args.operation == "studio-source"
                    else verifier.connection()
                )
        print(json.dumps({
            "status": "verified-read-only-dependency", "operation": args.operation,
            "read_only": True, "function_called": False, "business_acceptance_proven": False,
            "function_retirement_ready": False, "result": result,
        }, indent=2))
        return 0
    except admin.AdministrationFailure as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "failed", "error": "invalid_dependency_configuration"}))
    except (AzureError, requests.RequestException) as error:
        print(json.dumps({"status": "failed", "error": "dependency_read_unavailable",
                          "exception_type": type(error).__name__}))
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    raise SystemExit(main())
