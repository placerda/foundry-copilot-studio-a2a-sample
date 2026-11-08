"""Update only an existing approved Gateway and its exact application grants.

Run with the explicitly selected AZURE_CONFIG_DIR. Credentials and encryption
keys stay in process and protected Azure settings, never command output/files.
"""

import argparse
import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from azure.identity import AzureCliCredential
import requests


class Cloud:
    def __init__(self):
        self.credential = AzureCliCredential(process_timeout=30)
        self.session = requests.Session()

    def call(self, method, url, payload=None):
        resource = "https://graph.microsoft.com" if url.startswith("https://graph.microsoft.com/") else "https://management.azure.com"
        token = self.credential.get_token(resource + "/.default").token
        response = self.session.request(
            method, url, json=payload, headers={"Authorization": "Bearer " + token},
            timeout=(5, 45),
        )
        if response.status_code not in (200, 201, 202, 204):
            raise RuntimeError(f"Approved Gateway operation failed with HTTP {response.status_code}")
        return response.json() if response.content else {}


def configure(
    cloud, *, subscription, resource_group, function_name, oauth_client_id,
    guest_oid, requester_client_id, records, apply=False,
):
    subscription, oauth_client_id, guest_oid, requester_client_id = (
        str(UUID(v)) for v in (subscription, oauth_client_id, guest_oid, requester_client_id)
    )
    site = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/"
        f"{resource_group}/providers/Microsoft.Web/sites/{function_name}"
    )
    suffix = "?api-version=2024-04-01"
    live = cloud.call("GET", site + suffix)
    host = live["properties"]["defaultHostName"]
    gateway_oid = str(UUID(live["identity"]["principalId"]))
    setting_url = site + "/config/appsettings"
    settings = cloud.call("POST", setting_url + "/list" + suffix)["properties"]
    bindings = json.loads(settings["CONTOSO_PUBLICATION_BINDINGS"])
    if bindings["subscription_id"] != subscription or bindings["resource_group"] != resource_group:
        raise ValueError("Existing Gateway bindings do not match selected project")
    tenant = settings["CONTOSO_ENTRA_TENANT_ID"]
    graph = "https://graph.microsoft.com/v1.0"
    api = cloud.call("GET", graph + f"/servicePrincipals(appId='{settings['CONTOSO_ENTRA_API_CLIENT_ID']}')")
    requester = cloud.call("GET", graph + f"/servicePrincipals(appId='{requester_client_id}')")
    selected = {}
    for service in ("contoso-foundry-sdk-agent", "contoso-foundry-agent", "contoso-knowledge-agent"):
        matches = [r for r in records if r["name"] == service and r["status"] == "active"]
        if len(matches) != 1:
            raise ValueError("Exactly one actual active record per selected service is required")
        selected[service] = matches[0]
    agents = []
    for service in ("contoso-foundry-sdk-agent", "contoso-foundry-agent"):
        identity = selected[service]["instance_identity"]
        oid, client = str(UUID(identity["principal_id"])), str(UUID(identity["client_id"]))
        principal = cloud.call("GET", graph + "/servicePrincipals/" + oid)
        if principal["appId"] != client:
            raise ValueError("Actual agent client/OID pair differs from directory")
        agents.append((client, oid))
    values = {**bindings["environment_values"], **settings}
    # Today's approved business scope is one native-A2A Router/STUDIO Harness.
    agents = [agents[1]]
    values.update(
        CONTOSO_GATEWAY_PHASE="connections", CONTOSO_TENANT_ID=tenant,
        CONTOSO_GATEWAY_AUDIENCE=settings["CONTOSO_ENTRA_API_CLIENT_ID"],
        CONTOSO_AWS_REQUESTER_CLIENT_ID=requester_client_id, CONTOSO_AWS_REQUESTER_OID=requester["id"],
        CONTOSO_MANAGED_AGENT_CLIENT_ID=agents[0][0], CONTOSO_MANAGED_AGENT_OID=agents[0][1],
        CONTOSO_MANAGED_AGENT_IDENTITIES="[]",
        CONTOSO_CONNECTION_PROVIDER_IDS='["studio-copilot-harness"]',
        CONTOSO_GATEWAY_A2A_ENABLED="false",
        CONTOSO_GATEWAY_IDENTITY_OID=gateway_oid,
        CONTOSO_PLATFORM_CALLER_IDS=json.dumps([gateway_oid]),
        CONTOSO_ROUTER_SDK_AGENT_NAME=selected["contoso-foundry-sdk-agent"]["name"],
        CONTOSO_ROUTER_A2A_ENDPOINT=selected["contoso-foundry-agent"]["agent_endpoints"]["a2a"],
        CONTOSO_KNOWLEDGE_A2A_ENDPOINT=selected["contoso-knowledge-agent"]["agent_endpoints"]["a2a"],
        CONTOSO_OAUTH_CLIENT_ID=oauth_client_id, CONTOSO_OAUTH_GUEST_OID=guest_oid,
        CONTOSO_OAUTH_REDIRECT_URI=f"https://{host}/api/oauth/callback",
        CONTOSO_OAUTH_CONTAINER_URL=bindings["blob_endpoint"].rstrip("/") + "/encrypted-tokens",
        CONTOSO_OAUTH_STATE_NAMESPACE="contoso-gateway-guest-v1",
    )
    role_ids = {
        role["value"]: role["id"] for role in api["appRoles"]
        if role["isEnabled"] and "Application" in role["allowedMemberTypes"]
    }
    grants = [(requester["id"], "CONTOSO.Invoke"), *((oid, "CONTOSO.Connection.Use") for _, oid in agents)]
    changes = []
    for principal_id, role in grants:
        role_id = role_ids[role]
        existing = cloud.call("GET", graph + f"/servicePrincipals/{principal_id}/appRoleAssignments")["value"]
        if not any(r["resourceId"] == api["id"] and r["appRoleId"] == role_id for r in existing):
            changes.append((graph + f"/servicePrincipals/{principal_id}/appRoleAssignments", {
                "principalId": principal_id, "resourceId": api["id"], "appRoleId": role_id,
            }))
    oauth_url = graph + f"/applications(appId='{oauth_client_id}')"
    oauth = cloud.call("GET", oauth_url)
    redirects = oauth.get("publicClient", {}).get("redirectUris", [])
    callback = values["CONTOSO_OAUTH_REDIRECT_URI"]
    oauth_change = callback not in redirects
    auth_url = site + "/config/authsettingsV2" + suffix
    auth = cloud.call("GET", auth_url)["properties"]
    if not auth["platform"]["enabled"] or not auth["globalValidation"]["requireAuthentication"]:
        raise ValueError("Existing Gateway must retain enabled authenticated Easy Auth")
    auth["globalValidation"]["excludedPaths"] = ["/api/oauth/callback"]
    policy = auth["identityProviders"]["azureActiveDirectory"]["validation"]
    policy["defaultAuthorizationPolicy"] = {
        "allowedApplications": list(dict.fromkeys([
            settings["CONTOSO_AUTHORING_CLIENT_ID"], requester_client_id, *(client for client, _ in agents),
        ])),
        "allowedPrincipals": {"identities": list(dict.fromkeys([
            settings["CONTOSO_AUTHORING_ADMIN_OID"], requester["id"], *(oid for _, oid in agents),
        ]))},
    }
    if apply:
        if not values.get("CONTOSO_OAUTH_STATE_KEY"):
            values["CONTOSO_OAUTH_STATE_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        for url, assignment in changes:
            cloud.call("POST", url, assignment)
        if oauth_change:
            cloud.call("PATCH", oauth_url, {"publicClient": {
                **oauth.get("publicClient", {}), "redirectUris": [*redirects, callback],
            }})
        cloud.call("PUT", setting_url + suffix, {"properties": values})
        cloud.call("PUT", auth_url, {"properties": auth})
    return {
        "applied": apply, "gateway": "https://" + host,
        "requester_pair": {"client_id": requester_client_id, "oid": requester["id"]},
        "agent_pairs": [{"client_id": c, "oid": o} for c, o in agents],
        "new_exact_role_assignments": len(changes),
        "oauth_callback": callback, "preserved_redirects": redirects,
        "anonymous_paths": ["/api/oauth/callback"],
        "state_key_generated": apply and "CONTOSO_OAUTH_STATE_KEY" not in settings,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("subscription", "resource-group", "function-name", "oauth-client-id", "guest-oid", "requester-client-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = vars(parser.parse_args())
    args["records"] = json.loads(args["records"].read_text(encoding="utf-8-sig"))
    print(json.dumps(configure(Cloud(), **args), indent=2))
