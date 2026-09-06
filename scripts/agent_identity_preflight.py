"""Read-only permission preflight for the repo's identity deployment scripts.

Uses ONLY the existing deployment az login via AzureCliCredential. Reads actual
Graph appRoleAssignments on the CONFIGURATION MI and BLUEPRINT PRINCIPAL. The
operator's token scopes, MI appRoles, requiredResourceAccess, Azure RBAC roles,
and a successful caller GET do NOT establish the MI/blueprint's permissions.
Never grants app roles, consent, directory roles, federation or runtime permissions.

Clean tenant bootstrap is necessarily staged (there is no grant-on-create here):
1. Provision/adopt the configuration UAMI without enabling identity deployment.
2. An authorized tenant administrator grants its required Graph APPLICATION roles.
3. Run --stage bootstrap --managed-identity-principal-id <object-id>. This checks
   only bootstrap permissions, NOT readiness to run the full deployment.
4. Precreate the blueprint and its principal (Entra admin center or a separate
   administrator-controlled identity-only bootstrap). An administrator consents
   AgentIdentity.CreateAsManager and AgentIdentity.Read.All on that PRINCIPAL.
5. Run --stage deploy --managed-identity-principal-id <object-id>
   --blueprint-app-id <client-id> [--agent-identity-id <precreated-agent-id>]
   [--registry]. Only then enable the complete identity deployment, passing the
   precreated IDs to its existingBlueprintAppId/existingAgentIdentityId inputs.

The blueprint trusts the MI using FIC name mcp-agent-msi, issuer
https://login.microsoftonline.com/<tenant>/v2.0, subject=<MI object ID>, audience
api://AzureADTokenExchange. Drift is reported as planned reconciliation, not
silently treated as a successful token exchange. MI/blueprint token acquisition
can only be tested in the deployed compute context, not with a local user token.

Configuration MI requirements follow infra/core/identity/agentIdentityBlueprint.bicep:
AgentIdentityBlueprint.Create, .Read.All, .AddRemoveCreds.All,
.UpdateBranding.All (FIC PATCH), .UpdateAuthProperties.All,
AgentIdentityBlueprintPrincipal.Create and .Read.All. Adoption avoids Create
only when the blueprint, named FIC and blueprint principal already exist.
Registry publication has a DIFFERENT deployment credential/permission check.
--registry checks it explicitly; AGENT_REGISTRY_ENABLED=true also enables that
check in this CLI, so an opted-in deployment cannot accidentally omit it.

Directory inspection normally requires Application.Read.All delegated consent
and a supported directory-reader role on the operator. Typed blueprint/FIC reads
may additionally need AgentIdentityBlueprint.Read.All and the supported Agent ID
role/ownership. Denied, limited, missing or malformed directory data is a blocker,
not evidence that permissions are satisfied. No secrets or arbitrary errors print.

Exit codes: 0 selected stage's permission checks passed; 2 missing prerequisite/
permission; 3 inconclusive. --stage bootstrap can return 0 with deploymentReady
false: it must NEVER be used as the full-deployment gate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_registry import (  # noqa: E402
    API_CHOICES, GRAPH_APP_ID, AgentRegistryPublisher, GraphClient, RegistryError, guid,
)


DOCUMENTATION = (
    "https://learn.microsoft.com/en-us/entra/agent-id/create-blueprint",
    "https://learn.microsoft.com/en-us/graph/api/agentidentityblueprint-post?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/agentidentityblueprint-list?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/agentidentityblueprintprincipal-post?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/agentidentityblueprintprincipal-list?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/federatedidentitycredential-update?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/application-post-federatedidentitycredentials?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/application-list-federatedidentitycredentials?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/agentidentity-post?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/agentidentity-list?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/agentidentity-get?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list-approleassignments?view=graph-rest-1.0",
    "https://learn.microsoft.com/en-us/graph/api/serviceprincipal-get?view=graph-rest-1.0",
)
DIRECTORY_ACTION = (
    "Inspection was denied or incomplete; readiness is inconclusive. Use an approved "
    "deployment login with Application.Read.All and a supported directory-reader role "
    "to read service principals/appRoleAssignments, plus the required typed Agent ID "
    "read permissions/role or ownership. Never substitute caller scopes for MI grants."
)
GRANT_ACTION = (
    "Ask a Privileged Role Administrator to consent the missing least-privilege Graph "
    "APPLICATION permissions to this specific principal outside deployment, allow "
    "propagation, then rerun. This preflight does not grant permissions."
)
BOOTSTRAP_STEPS = [
    "Provision or adopt the configuration user-assigned MI first, with full identity deployment disabled; obtain its principal/object ID, not client ID or ARM resource ID.",
    "Have an authorized administrator consent the configuration MI Graph application permissions outside deployment; run the bootstrap stage preflight.",
    "Precreate/adopt the blueprint AND its blueprint principal in a separate controlled bootstrap (or Entra admin center), then consent the blueprint principal's Graph application permissions.",
    "Rerun the deploy stage with --blueprint-app-id (and --agent-identity-id for adoption). Pass the IDs to the Bicep adoption parameters; do not run the full deployment while deploymentReady is false.",
]


def _requirement(operation: str, least: list[str], *alternatives: list[str]) -> dict:
    return {"operation": operation, "leastPrivilege": least, "accepted": [least, *alternatives]}


def mi_requirements(*, blueprint_exists=False, fic_exists=False, principal_exists=False) -> list[dict]:
    bp_rw = ["AgentIdentityBlueprint.ReadWrite.All"]
    sp_rw = ["AgentIdentityBlueprintPrincipal.ReadWrite.All"]
    checks = [
        _requirement("blueprintReadAndDiscovery", ["AgentIdentityBlueprint.Read.All"], bp_rw),
        _requirement("federatedCredentialRead", ["AgentIdentityBlueprint.Read.All"], ["Application.Read.All"], ["Directory.Read.All"]),
        _requirement("federatedCredentialReconciliation", ["AgentIdentityBlueprint.AddRemoveCreds.All", "AgentIdentityBlueprint.UpdateBranding.All"], bp_rw, ["Directory.ReadWrite.All"]),
        _requirement("blueprintIdentifierUriAndScope", ["AgentIdentityBlueprint.UpdateAuthProperties.All"], bp_rw),
        _requirement("blueprintPrincipalRead", ["AgentIdentityBlueprintPrincipal.Read.All"], sp_rw),
    ]
    # The typed beta FIC POST permission table lists Blueprint.Create; the Entra
    # how-to also calls for AddRemoveCreds.All. The full reconciliation plan
    # deliberately covers both sources, matching the Bicep prerequisite comments.
    if not blueprint_exists or not fic_exists:
        checks.append(_requirement("blueprintOrFederatedCredentialCreate", ["AgentIdentityBlueprint.Create"], bp_rw))
    if not principal_exists:
        checks.append(_requirement("blueprintPrincipalCreate", ["AgentIdentityBlueprintPrincipal.Create"], sp_rw))
    return checks


def blueprint_requirements(*, adopting_agent=False) -> list[dict]:
    if adopting_agent:
        # GET by ID supports the manager permission or read permissions; it does
        # not require collection-discovery/create consent when adopting an ID.
        return [_requirement("readPrecreatedAgent", ["AgentIdentity.Read.All"], ["AgentIdentity.CreateAsManager"], ["Application.Read.All"])]
    return [
        _requirement("createChildAsBlueprintManager", ["AgentIdentity.CreateAsManager"], ["AgentIdentity.Create.All"], ["AgentIdentity.ReadWrite.All"]),
        _requirement("discoverExistingChildren", ["AgentIdentity.Read.All"], ["AgentIdentity.ReadWrite.All"]),
    ]


class GrantReader:
    """Resolve role IDs against the tenant's actual Microsoft Graph service principal."""

    def __init__(self, graph: GraphClient):
        self.graph = graph
        catalog = graph.request("GET", f"/v1.0/servicePrincipals(appId='{GRAPH_APP_ID}')?$select=id,appId,appRoles")
        self.resource_id = guid(catalog.get("id"), "Graph resource principal ID")
        if catalog.get("appId") != GRAPH_APP_ID or not isinstance(catalog.get("appRoles"), list):
            raise RegistryError("inconclusive_directory", "Graph application-role catalog is missing or limited.")
        self.roles: dict[str, tuple[str, bool]] = {}
        for role in catalog["appRoles"]:
            if not isinstance(role, dict) or not isinstance(role.get("value"), str) or not isinstance(role.get("allowedMemberTypes"), list) or type(role.get("isEnabled")) is not bool:
                raise RegistryError("inconclusive_directory", "Graph application-role catalog contains incomplete role definitions.")
            role_id = guid(role.get("id"), "Graph role ID")
            if role_id in self.roles:
                raise RegistryError("inconclusive_directory", "Graph role catalog is ambiguous.")
            self.roles[role_id] = (role["value"], role["isEnabled"] and "Application" in role["allowedMemberTypes"])
        self.available = {name for name, enabled in self.roles.values() if enabled}

    def assignments(self, principal_id: str) -> set[str]:
        permissions = set()
        path = f"/v1.0/servicePrincipals/{principal_id}/appRoleAssignments?$select=appRoleId,resourceId,principalId"
        for assignment in self.graph.collection(path):
            if guid(assignment.get("principalId"), "Assignment principal ID") != principal_id:
                raise RegistryError("inconclusive_directory", "Role assignment response identifies a different principal.")
            resource = guid(assignment.get("resourceId"), "Assignment resource ID")
            role_id = guid(assignment.get("appRoleId"), "Assigned app role ID")
            if resource != self.resource_id or role_id == "00000000-0000-0000-0000-000000000000":
                continue  # Another API/default access is not a Microsoft Graph permission.
            if role_id not in self.roles:
                raise RegistryError("inconclusive_directory", "Assigned Graph role is absent from the role catalog; check replication/tenant availability.")
            name, enabled = self.roles[role_id]
            if enabled:
                permissions.add(name)
        return permissions

    def evaluate(self, principal_id: str, plan: list[dict]) -> dict:
        assigned = self.assignments(principal_id)
        checks = []
        for requirement in plan:
            accepted = requirement["accepted"]
            matching = next((group for group in accepted if set(group) <= assigned), None)
            available = any(set(group) <= self.available for group in accepted)
            checks.append({
                "operation": requirement["operation"],
                "leastPrivilege": requirement["leastPrivilege"],
                "satisfiedBy": matching or [],
                "status": "satisfied" if matching else ("missing_permissions" if available else "inconclusive"),
            })
        missing = sorted({p for c in checks if c["status"] != "satisfied" for p in c["leastPrivilege"] if p not in assigned})
        status = "inconclusive" if any(c["status"] == "inconclusive" for c in checks) else ("missing_permissions" if missing else "satisfied")
        return {
            "principalId": principal_id, "status": status, "permissionChecks": checks,
            "missingPermissions": missing,
            "action": (
                "Actual Graph application-role assignments verified; token issuance and tenant policies remain external checks." if status == "satisfied"
                else "Required role definitions are unavailable in this tenant. Verify Agent ID rollout/licensing and catalog replication before consent; do not invent role IDs." if status == "inconclusive"
                else GRANT_ACTION
            ),
        }


def _active_principal(record: dict, expected_id: str | None = None):
    value = guid(record.get("id"), "Principal ID")
    if expected_id and value != expected_id:
        raise RegistryError("inconclusive_directory", "Directory returned a different principal.")
    if type(record.get("accountEnabled")) is not bool:
        raise RegistryError("inconclusive_directory", "Principal accountEnabled is missing or unreadable; readiness cannot be verified.")
    if not record["accountEnabled"]:
        raise RegistryError("principal_unavailable", "Principal is disabled; administrator review is required.")
    if record.get("disabledByMicrosoftStatus") not in (None, "", "NotDisabled"):
        raise RegistryError("principal_unavailable", "Principal has a Microsoft administrative restriction; no automatic enable/unblock is allowed.")


def _blueprint_context(graph: GraphClient, app_id: str, mi_id: str | None) -> dict:
    application = graph.request("GET", f"/v1.0/applications(appId='{app_id}')/microsoft.graph.agentIdentityBlueprint?$select=id,appId,disabledByMicrosoftStatus")
    object_id = guid(application.get("id"), "Blueprint application object ID")
    if application.get("appId") != app_id:
        raise RegistryError("inconclusive_directory", "Blueprint application/client ID mismatch.")
    if application.get("disabledByMicrosoftStatus") not in (None, "", "NotDisabled"):
        raise RegistryError("principal_unavailable", "Blueprint is administratively restricted; review it in Entra.")
    query = urlencode({"$filter": f"appId eq '{app_id}'", "$select": "id,appId,accountEnabled,disabledByMicrosoftStatus"})
    principals = graph.collection("/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal?" + query)
    if len(principals) > 1 or any(p.get("appId") != app_id for p in principals):
        raise RegistryError("inconclusive_directory", "Blueprint principal discovery is ambiguous or returned a different application.")
    if principals:
        _active_principal(principals[0])
    fics = graph.collection(f"/beta/applications/{object_id}/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials")
    if any(not isinstance(f.get("name"), str) for f in fics):
        raise RegistryError("inconclusive_directory", "FIC inspection returned limited information.")
    named = [f for f in fics if f.get("name") == "mcp-agent-msi"]
    if len(named) > 1:
        raise RegistryError("inconclusive_directory", "Multiple managed-identity FICs have the deployment's name.")
    caller = graph.caller()
    tenant = graph.tenant_id or (caller["tenantId"] if caller else None)
    if named and (not isinstance(named[0].get("issuer"), str) or not isinstance(named[0].get("subject"), str) or not isinstance(named[0].get("audiences"), list)):
        raise RegistryError("inconclusive_directory", "FIC inspection returned incomplete trust properties.")
    if named and not tenant:
        raise RegistryError("inconclusive_directory", "Cannot determine tenant for FIC validation; supply --tenant-id.")
    matches = bool(named and mi_id and named[0]["issuer"] == f"https://login.microsoftonline.com/{tenant}/v2.0" and named[0]["subject"] == mi_id and named[0]["audiences"] == ["api://AzureADTokenExchange"] and not named[0].get("claimsMatchingExpression"))
    return {
        "objectId": object_id, "principalId": guid(principals[0]["id"], "Blueprint principal ID") if principals else None,
        "ficExists": bool(named), "ficMatches": matches,
    }


def _failure(component: str, error: RegistryError, required=True) -> dict:
    return {
        "component": component, "status": "missing_prerequisite" if error.http_status == 404 or error.code == "principal_unavailable" else "inconclusive",
        "requiredForSelectedStage": required, "error": error.as_dict(),
        "action": DIRECTORY_ACTION if error.http_status != 404 else "The specified principal/blueprint or typed API was not found. Verify tenant, preview availability and precreated IDs; follow the staged bootstrap steps.",
    }


def identity_preflight(
    graph: GraphClient, *, managed_identity_principal_id: str | None = None,
    blueprint_app_id: str | None = None, agent_identity_id: str | None = None,
    stage: str = "deploy", registry: bool = False, registry_api: str = "agent365",
    saved_registry_id: str | None = None, managed_by_app_id: str | None = None,
) -> dict[str, Any]:
    if stage not in ("bootstrap", "deploy"):
        raise RegistryError("invalid_configuration", "Stage must be bootstrap or deploy.")
    mi_id = guid(managed_identity_principal_id, "Managed identity principal/object ID") if managed_identity_principal_id else None
    blueprint_id = guid(blueprint_app_id, "Blueprint application/client ID") if blueprint_app_id else None
    agent_id = guid(agent_identity_id, "Precreated agent identity ID") if agent_identity_id else None
    checks: list[dict] = []
    context = None
    context_error = None
    if blueprint_id:
        try:
            context = _blueprint_context(graph, blueprint_id, mi_id)
        except RegistryError as error:
            context_error = error
    reader = None
    reader_error = None
    if mi_id or blueprint_id:
        try:
            reader = GrantReader(graph)
        except RegistryError as error:
            reader_error = error

    if mi_id is None:
        checks.append({"component": "configurationManagedIdentity", "status": "missing_prerequisite", "requiredForSelectedStage": True, "action": BOOTSTRAP_STEPS[0]})
    elif reader_error:
        checks.append(_failure("configurationManagedIdentity", reader_error))
    elif blueprint_id and context_error:
        checks.append(_failure("configurationManagedIdentity", context_error))
    else:
        try:
            principal = graph.request("GET", f"/v1.0/servicePrincipals/{mi_id}?$select=id,appId,servicePrincipalType,accountEnabled,disabledByMicrosoftStatus")
            _active_principal(principal, mi_id)
            if principal.get("servicePrincipalType") != "ManagedIdentity":
                raise RegistryError("inconclusive_directory", "Configuration principal is not a managed identity (or its type is unreadable).")
            plan = mi_requirements(
                blueprint_exists=context is not None,
                fic_exists=bool(context and context["ficExists"]),
                principal_exists=bool(context and context["principalId"]),
            )
            check = reader.evaluate(mi_id, plan)
            check.update(component="configurationManagedIdentity", requiredForSelectedStage=True)
            if context:
                check["federation"] = "matchesConfiguration" if context["ficMatches"] else "deploymentWillReconcile"
            checks.append(check)
        except RegistryError as error:
            checks.append(_failure("configurationManagedIdentity", error))

    required = stage == "deploy"
    if not blueprint_id:
        checks.append({"component": "blueprintPrincipal", "status": "missing_prerequisite", "requiredForSelectedStage": required, "action": "Blueprint/principal do not yet exist or no ID was supplied; their grants CANNOT be preconsented by this deployment. " + BOOTSTRAP_STEPS[2]})
    elif context_error or reader_error:
        checks.append(_failure("blueprintPrincipal", context_error or reader_error, required))
    elif not context["principalId"]:
        checks.append({"component": "blueprintPrincipal", "status": "missing_prerequisite", "requiredForSelectedStage": required, "blueprintAppId": blueprint_id, "action": "Blueprint exists but its principal does not. Precreate the principal and obtain application-role consent before the deploy stage; MI permissions alone cannot satisfy this."})
    else:
        try:
            check = reader.evaluate(context["principalId"], blueprint_requirements(adopting_agent=agent_id is not None))
            check.update(component="blueprintPrincipal", blueprintAppId=blueprint_id, blueprintObjectId=context["objectId"], requiredForSelectedStage=required)
            checks.append(check)
        except RegistryError as error:
            checks.append(_failure("blueprintPrincipal", error, required))

    if agent_id:
        try:
            if not blueprint_id:
                raise RegistryError("inconclusive_directory", "Precreated agent adoption also requires --blueprint-app-id to verify its parent.")
            agent = graph.request("GET", f"/v1.0/servicePrincipals/{agent_id}/microsoft.graph.agentIdentity?$select=id,appId,agentIdentityBlueprintId,accountEnabled,disabledByMicrosoftStatus")
            _active_principal(agent, agent_id)
            if agent.get("agentIdentityBlueprintId") != blueprint_id or (agent.get("appId") and agent["appId"] != agent_id):
                raise RegistryError("inconclusive_directory", "Precreated agent belongs to another blueprint or has an unexpected app/object ID mismatch.")
            checks.append({"component": "precreatedAgent", "agentIdentityId": agent_id, "status": "satisfied", "requiredForSelectedStage": required, "action": "Explicit identity adoption verified; no identity creation permission was inferred from the caller."})
        except RegistryError as error:
            checks.append(_failure("precreatedAgent", error, required))
    if registry:
        managing_app = guid(managed_by_app_id, "Registry managing application ID") if managed_by_app_id else None
        check = AgentRegistryPublisher(graph, api=registry_api).preflight(saved_id=saved_registry_id, managed_by=managing_app)
        check["requiredForSelectedStage"] = True
        checks.append(check)
    active = [c for c in checks if c["requiredForSelectedStage"]]
    status = "inconclusive" if any(c["status"] == "inconclusive" for c in active) else (
        "blocked" if any(c["status"] != "satisfied" for c in active) else "satisfied"
    )
    return {
        "component": "agentIdentityDeployment", "readOnly": True, "stage": stage,
        "status": status, "deploymentReady": stage == "deploy" and status == "satisfied",
        "checks": checks, "nextSteps": BOOTSTRAP_STEPS,
        "limitations": [
            "A passed check verifies configured permission assignments, not MI-to-blueprint token issuance, tenant policy, API rollout, Azure RBAC or AKS federation.",
            "Sponsors (valid users, dynamic groups or Microsoft 365 groups) and optional owners must be supplied separately; groups cannot be owners.",
            "Registry publication is opt-in and uses a separate deployment credential. Neither identity creation nor registry publication approves runtime actions.",
            "Bootstrap-stage success is not a full-deployment gate. Grants may have replication delays; no grants or secrets are created by this command.",
        ],
    }


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise RegistryError("invalid_arguments", "Invalid command-line arguments; use --help. Values have been suppressed.")


def main(argv: list[str] | None = None) -> int:
    try:
        registry_flag = os.getenv("AGENT_REGISTRY_ENABLED", "false").strip().lower()
        if registry_flag not in ("true", "false", "1", "0"):
            raise RegistryError("invalid_configuration", "AGENT_REGISTRY_ENABLED must be true/false or 1/0.")
        parser = SafeArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--managed-identity-principal-id", default=os.getenv("AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID"))
        parser.add_argument("--blueprint-app-id", default=os.getenv("AGENT_IDENTITY_BLUEPRINT_APP_ID"))
        parser.add_argument("--agent-identity-id", "--existing-agent-identity-id", default=os.getenv("AGENT_IDENTITY_APP_ID"))
        parser.add_argument("--tenant-id", default=os.getenv("AZURE_TENANT_ID"))
        parser.add_argument("--stage", choices=("bootstrap", "deploy"), default="deploy")
        parser.add_argument("--registry", action="store_true", default=registry_flag in ("true", "1"), help="Check independent publisher permissions (also automatic when AGENT_REGISTRY_ENABLED=true).")
        parser.add_argument("--registry-api", choices=API_CHOICES, default=os.getenv("AGENT_REGISTRY_API", "agent365"))
        parser.add_argument("--registry-id", default=os.getenv("AGENT_REGISTRY_ID"))
        parser.add_argument("--managed-by-app-id", default=os.getenv("AGENT_REGISTRY_MANAGED_BY_APP_ID"))
        args = parser.parse_args(argv)
        with GraphClient(tenant_id=args.tenant_id) as graph:
            report = identity_preflight(
                graph, managed_identity_principal_id=args.managed_identity_principal_id,
                blueprint_app_id=args.blueprint_app_id, agent_identity_id=args.agent_identity_id,
                stage=args.stage, registry=args.registry, registry_api=args.registry_api,
                saved_registry_id=args.registry_id, managed_by_app_id=args.managed_by_app_id,
            )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "satisfied" else (3 if report["status"] == "inconclusive" else 2)
    except RegistryError as error:
        print(json.dumps({"status": "blocked", "deploymentReady": False, "error": error.as_dict()}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "inconclusive", "deploymentReady": False, "error": {"code": "unexpected_error", "message": "Preflight could not inspect deployment permissions; diagnostic content is suppressed."}}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())