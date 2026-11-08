"""Provision scoped CONTOSO identities without generating or persisting credentials."""

import argparse
import json
from uuid import UUID, uuid4

from azure.identity import AzureCliCredential
import requests


class Graph:
    def __init__(self, tenant: str):
        self.credential = AzureCliCredential(tenant_id=tenant)
        self.session = requests.Session()
        self.session.trust_env = False

    def call(self, method: str, path: str, *, body=None, params=None):
        token = self.credential.get_token("https://graph.microsoft.com/.default")
        response = self.session.request(
            method, f"https://graph.microsoft.com/v1.0/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token.token}"},
            json=body, params=params, timeout=(10, 40), allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            request_id = response.headers.get("request-id", "unavailable")
            raise RuntimeError(f"Graph {method} failed: HTTP {response.status_code}, request {request_id}")
        return response.json() if response.content else None

    def find(self, collection: str, expression: str):
        result = self.call("GET", collection, params={"$filter": expression})
        values = result["value"]
        if result.get("@odata.nextLink") or len(values) > 1:
            raise RuntimeError(f"Ambiguous CONTOSO identity lookup: {collection}")
        return values[0] if values else None


def principal(graph: Graph, app_id: str):
    existing = graph.find("servicePrincipals", f"appId eq '{app_id}'")
    return existing if existing is not None else graph.call(
        "POST", "servicePrincipals", body={"appId": app_id},
    )


def assign_role(graph: Graph, resource, subject: str, value: str):
    role = next(role for role in resource["appRoles"] if role["value"] == value and role["isEnabled"])
    assignments = graph.call("GET", f"servicePrincipals/{resource['id']}/appRoleAssignedTo")
    if assignments.get("@odata.nextLink"):
        raise RuntimeError("Unexpected pagination in new CONTOSO role assignments")
    if any(item["principalId"] == subject and item["appRoleId"] == role["id"]
           for item in assignments["value"]):
        return
    graph.call("POST", f"servicePrincipals/{resource['id']}/appRoleAssignedTo", body={
        "principalId": subject, "resourceId": resource["id"], "appRoleId": role["id"],
    })


def grant_scope(graph: Graph, client, resource, subject: str, scope: str):
    result = graph.call("GET", "oauth2PermissionGrants", params={
        "$filter": f"resourceId eq '{resource['id']}'", "$top": 100,
    })
    if result.get("@odata.nextLink"):
        raise RuntimeError("Unexpected pagination in new CONTOSO delegated grants")
    matching = [
        item for item in result["value"] if item["clientId"] == client["id"]
        and item.get("principalId") == subject and item["consentType"] == "Principal"
    ]
    if len(matching) > 1:
        raise RuntimeError("Ambiguous principal-only CONTOSO delegated grant")
    existing = matching[0] if matching else None
    if existing is None:
        graph.call("POST", "oauth2PermissionGrants", body={
            "clientId": client["id"], "resourceId": resource["id"],
            "principalId": subject, "consentType": "Principal", "scope": scope,
        })
    elif scope not in existing["scope"].split():
        scopes = sorted(set(existing["scope"].split()) | {scope})
        graph.call("PATCH", f"oauth2PermissionGrants/{existing['id']}", body={"scope": " ".join(scopes)})


def setup(tenant: str, guest: str, spa_id: str):
    graph = Graph(tenant)
    admin_id = graph.call("GET", "me", params={"$select": "id"})["id"]
    name = "Contoso Gateway API"
    api = graph.find("applications", f"displayName eq '{name}'")
    if api is None:
        roles = [
            {"id": str(uuid4()), "value": value, "displayName": value,
             "description": description, "isEnabled": True, "allowedMemberTypes": [member]}
            for value, member, description in (
                ("CONTOSO.Invoke", "Application", "Invoke the approved CONTOSO Routers"),
                ("CONTOSO.Connection.Use", "Application", "Use the approved demo STUDIO connection"),
                ("CONTOSO.DemoUser", "User", "Connect the selected demo guest identity"),
                ("CONTOSO.DeploymentAdmin", "User", "Prepare the private CONTOSO deployment"),
            )
        ]
        scopes = [
            {"id": str(uuid4()), "value": value, "type": "Admin", "isEnabled": True,
             "adminConsentDisplayName": value, "adminConsentDescription": description}
            for value, description in (
                ("CONTOSO.Connect", "Connect the selected guest to the Contoso demo agents"),
                ("CONTOSO.Deploy", "Prepare the approved private CONTOSO deployment"),
            )
        ]
        api = graph.call("POST", "applications", body={
            "displayName": name, "signInAudience": "AzureADMyOrg",
            "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes},
            "appRoles": roles,
            "optionalClaims": {"accessToken": [{"name": "idtyp", "essential": False}]},
        })
        graph.call("PATCH", f"applications/{api['id']}", body={
            "identifierUris": [f"api://{api['appId']}"],
        })
    if (api["signInAudience"] != "AzureADMyOrg"
            or api["api"]["requestedAccessTokenVersion"] != 2
            or set(role["value"] for role in api["appRoles"]) != {
                "CONTOSO.Invoke", "CONTOSO.Connection.Use", "CONTOSO.DemoUser", "CONTOSO.DeploymentAdmin",
            }
            or set(scope["value"] for scope in api["api"]["oauth2PermissionScopes"]) != {
                "CONTOSO.Connect", "CONTOSO.Deploy",
            }):
        raise RuntimeError("Existing CONTOSO API does not match the approved identity contract")
    resource = principal(graph, api["appId"])
    if not resource.get("appRoleAssignmentRequired"):
        graph.call("PATCH", f"servicePrincipals/{resource['id']}", body={
            "appRoleAssignmentRequired": True,
        })
    requester = graph.find("applications", "displayName eq 'Contoso AWS Agent'")
    if requester is None:
        requester = graph.call("POST", "applications", body={
            "displayName": "Contoso AWS Agent", "signInAudience": "AzureADMyOrg",
        })
    if requester["signInAudience"] != "AzureADMyOrg":
        raise RuntimeError("Existing requester must be single-tenant")
    requester_sp = principal(graph, requester["appId"])
    assign_role(graph, resource, requester_sp["id"], "CONTOSO.Invoke")
    assign_role(graph, resource, guest, "CONTOSO.DemoUser")
    assign_role(graph, resource, admin_id, "CONTOSO.DeploymentAdmin")
    spa = principal(graph, spa_id)
    cli = principal(graph, "04b07795-8ddb-461a-bbee-02f9e1bf7b46")
    grant_scope(graph, spa, resource, guest, "CONTOSO.Connect")
    grant_scope(graph, cli, resource, admin_id, "CONTOSO.Deploy")
    verified = graph.call("GET", f"applications/{api['id']}")
    return {
        "gateway_api_app_id": api["appId"], "gateway_api_object_id": api["id"],
        "gateway_api_principal_id": resource["id"],
        "requester_app_id": requester["appId"], "requester_principal_id": requester_sp["id"],
        "deployment_admin_oid": admin_id, "guest_oid": guest,
        "gateway_roles": {role["value"]: role["id"] for role in verified["appRoles"]},
        "gateway_scopes": {
            scope["value"]: scope["id"] for scope in verified["api"]["oauth2PermissionScopes"]
        },
        "credentials_created": False, "tenant_wide_consent": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, type=lambda value: str(UUID(value)))
    parser.add_argument("--guest", required=True, type=lambda value: str(UUID(value)))
    parser.add_argument("--spa-client-id", required=True, type=lambda value: str(UUID(value)))
    options = parser.parse_args()
    print(json.dumps(setup(options.tenant, options.guest, options.spa_client_id), indent=2))
