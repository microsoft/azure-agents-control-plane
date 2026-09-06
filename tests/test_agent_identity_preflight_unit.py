"""Offline permission preflight tests; only mocked read-only HTTP is permitted."""

import base64
from copy import deepcopy
import json
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit
from uuid import UUID

import pytest
from azure.core.credentials import AccessToken

from scripts import agent_identity_preflight as preflight
from src import agent_registry as registry


TENANT = "11111111-1111-1111-1111-111111111111"
MI = "22222222-2222-2222-2222-222222222222"
BLUEPRINT = "33333333-3333-3333-3333-333333333333"
BLUEPRINT_OBJECT = "44444444-4444-4444-4444-444444444444"
BLUEPRINT_PRINCIPAL = "55555555-5555-5555-5555-555555555555"
AGENT = "66666666-6666-6666-6666-666666666666"
GRAPH_PRINCIPAL = "77777777-7777-7777-7777-777777777777"
CALLER = "88888888-8888-8888-8888-888888888888"
CLIENT = "99999999-9999-9999-9999-999999999999"
GRAPH_CATALOG_PATH = f"/v1.0/servicePrincipals(appId='{registry.GRAPH_APP_ID}')"
BLUEPRINT_PATH = f"/v1.0/applications(appId='{BLUEPRINT}')/microsoft.graph.agentIdentityBlueprint"
PRINCIPALS_PATH = "/v1.0/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal"
FIC_PATH = f"/beta/applications/{BLUEPRINT_OBJECT}/microsoft.graph.agentIdentityBlueprint/federatedIdentityCredentials"
MI_ROLES = {
    "AgentIdentityBlueprint.Create", "AgentIdentityBlueprint.Read.All",
    "AgentIdentityBlueprint.AddRemoveCreds.All", "AgentIdentityBlueprint.UpdateBranding.All",
    "AgentIdentityBlueprint.UpdateAuthProperties.All", "AgentIdentityBlueprintPrincipal.Create",
    "AgentIdentityBlueprintPrincipal.Read.All",
}
BLUEPRINT_ROLES = {"AgentIdentity.CreateAsManager", "AgentIdentity.Read.All"}
PUBLISHER_ROLES = {"AgentRegistration.Read.All", "AgentRegistration.ReadWrite.All"}


class Response:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self.body = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.body


class Directory:
    def __init__(self):
        all_plans = preflight.mi_requirements() + preflight.blueprint_requirements() + preflight.blueprint_requirements(adopting_agent=True)
        names = sorted({p for plan in all_plans for group in plan["accepted"] for p in group})
        self.role_ids = {name: str(UUID(int=i + 1000)) for i, name in enumerate(names)}
        self.catalog = {
            "id": GRAPH_PRINCIPAL, "appId": registry.GRAPH_APP_ID,
            "appRoles": [
                {"id": value, "value": name, "allowedMemberTypes": ["Application"], "isEnabled": True}
                for name, value in self.role_ids.items()
            ],
        }
        self.mi = {"id": MI, "appId": CLIENT, "accountEnabled": True, "servicePrincipalType": "ManagedIdentity"}
        self.application = {"id": BLUEPRINT_OBJECT, "appId": BLUEPRINT}
        self.principals = [{"id": BLUEPRINT_PRINCIPAL, "appId": BLUEPRINT, "accountEnabled": True}]
        self.agent = {"id": AGENT, "agentIdentityBlueprintId": BLUEPRINT, "accountEnabled": True}
        self.fics = [{
            "id": str(UUID(int=999)), "name": "mcp-agent-msi",
            "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "subject": MI, "audiences": ["api://AzureADTokenExchange"],
        }]
        self.grants = {MI: self.assignments(MI, MI_ROLES), BLUEPRINT_PRINCIPAL: self.assignments(BLUEPRINT_PRINCIPAL, BLUEPRINT_ROLES)}
        self.overrides = {}
        self.calls = []

    def assignments(self, principal, roles, resource=GRAPH_PRINCIPAL):
        return [{"principalId": principal, "appRoleId": self.role_ids[name], "resourceId": resource} for name in sorted(roles)]

    def request(self, method, url, **options):
        assert method == "GET", "Preflight must NEVER change tenant state"
        assert options["timeout"] == (5, 20) and options["allow_redirects"] is False
        assert "json" not in options
        parsed = urlsplit(url)
        assert parsed.netloc == "graph.microsoft.com" and parsed.scheme == "https"
        path = unquote(parsed.path)
        self.calls.append((method, url))
        override = self.overrides.get(path, [])
        if override:
            value = override.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if path == GRAPH_CATALOG_PATH:
            return Response(deepcopy(self.catalog))
        if path == BLUEPRINT_PATH:
            return Response(deepcopy(self.application))
        if path == PRINCIPALS_PATH:
            return Response({"value": deepcopy(self.principals)})
        if path == FIC_PATH:
            return Response({"value": deepcopy(self.fics)})
        if path == f"/v1.0/servicePrincipals/{MI}":
            return Response(deepcopy(self.mi))
        if path == f"/v1.0/servicePrincipals/{AGENT}/microsoft.graph.agentIdentity":
            return Response(deepcopy(self.agent))
        for principal, grants in self.grants.items():
            if path == f"/v1.0/servicePrincipals/{principal}/appRoleAssignments":
                return Response({"value": deepcopy(grants)})
        raise AssertionError("Unexpected directory endpoint; no fallback is permitted")


def graph_client(directory=None, *, caller_permissions=None, delegated=True):
    directory = directory or Directory()
    permissions = sorted(PUBLISHER_ROLES if caller_permissions is None else caller_permissions)
    claims = {"aud": registry.GRAPH_ORIGIN, "tid": TENANT, "oid": CALLER, "appid": CLIENT}
    claims["scp" if delegated else "roles"] = " ".join(permissions) if delegated else permissions
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    credential = Mock()
    credential.get_token.return_value = AccessToken("header." + encoded + ".signature", 5000)
    session = Mock()
    session.request.side_effect = directory.request
    return registry.GraphClient(credential=credential, session=session, tenant_id=TENANT), directory


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in list(preflight.os.environ):
        if key.startswith(("AGENT_REGISTRY_", "AGENT_IDENTITY_")) or key in ("AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(registry.requests.Session, "send", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(registry, "AzureCliCredential", Mock(side_effect=AssertionError("Unexpected Azure CLI")))
    monkeypatch.setattr(registry.time, "time", lambda: 1000)


def run(graph, **kwargs):
    return preflight.identity_preflight(graph, managed_identity_principal_id=MI, blueprint_app_id=BLUEPRINT, **kwargs)


def component(report, name):
    return next(c for c in report["checks"] if c["component"] == name)


def test_full_preflight_reads_actual_mi_and_blueprint_assignments_not_caller_scopes():
    graph, directory = graph_client(caller_permissions={"User.Read"})
    report = run(graph)
    assert report["status"] == "satisfied" and report["deploymentReady"] is True
    assert component(report, "configurationManagedIdentity")["principalId"] == MI
    assert component(report, "blueprintPrincipal")["principalId"] == BLUEPRINT_PRINCIPAL
    assert component(report, "configurationManagedIdentity")["federation"] == "matchesConfiguration"
    urls = [unquote(url) for _, url in directory.calls]
    assert any(f"/{MI}/appRoleAssignments" in url for url in urls)
    assert any(f"/{BLUEPRINT_PRINCIPAL}/appRoleAssignments" in url for url in urls)
    assert not any(f"/{CALLER}/appRoleAssignments" in url for url in urls)
    assert report["readOnly"] is True


def test_clean_tenant_without_mi_is_actionable_block_without_any_graph_call():
    graph, directory = graph_client()
    report = preflight.identity_preflight(graph)
    assert report["status"] == "blocked" and report["deploymentReady"] is False
    assert component(report, "configurationManagedIdentity")["status"] == "missing_prerequisite"
    assert "principal/object ID" in report["nextSteps"][0]
    assert not directory.calls


def test_bootstrap_stage_can_pass_but_explicitly_is_not_deployment_ready():
    graph, directory = graph_client()
    report = preflight.identity_preflight(graph, managed_identity_principal_id=MI, stage="bootstrap")
    assert report["status"] == "satisfied" and report["deploymentReady"] is False
    pending = component(report, "blueprintPrincipal")
    assert pending["status"] == "missing_prerequisite" and pending["requiredForSelectedStage"] is False
    assert not any("/applications" in url for _, url in directory.calls)
    operations = component(report, "configurationManagedIdentity")["permissionChecks"]
    assert any("AgentIdentityBlueprint.Create" in c["leastPrivilege"] for c in operations)
    assert any("AgentIdentityBlueprintPrincipal.Create" in c["leastPrivilege"] for c in operations)


def test_full_deploy_gate_cannot_pass_before_blueprint_principal_consent():
    graph, _ = graph_client()
    report = preflight.identity_preflight(graph, managed_identity_principal_id=MI)
    assert report["status"] == "blocked" and report["deploymentReady"] is False
    assert component(report, "blueprintPrincipal")["requiredForSelectedStage"] is True


@pytest.mark.parametrize("permission", sorted(MI_ROLES))
def test_missing_configuration_role_is_not_satisfied_by_operator_token(permission):
    graph, directory = graph_client(caller_permissions=MI_ROLES | BLUEPRINT_ROLES | PUBLISHER_ROLES)
    directory.grants[MI] = directory.assignments(MI, MI_ROLES - {permission})
    # The new-blueprint bootstrap path requires all seven comment-listed roles.
    report = preflight.identity_preflight(graph, managed_identity_principal_id=MI, stage="bootstrap")
    check = component(report, "configurationManagedIdentity")
    assert check["status"] == "missing_permissions"
    assert permission in check["missingPermissions"]
    assert report["status"] == "blocked" and report["deploymentReady"] is False


def test_app_roles_and_required_resource_access_are_not_actual_consent():
    graph, directory = graph_client(caller_permissions=MI_ROLES)
    directory.mi["appRoles"] = deepcopy(directory.catalog["appRoles"])
    directory.mi["requiredResourceAccess"] = [{"resourceAppId": registry.GRAPH_APP_ID, "resourceAccess": [{"id": directory.role_ids[p], "type": "Role"} for p in MI_ROLES]}]
    directory.grants[MI] = []
    report = preflight.identity_preflight(graph, managed_identity_principal_id=MI, stage="bootstrap")
    assert component(report, "configurationManagedIdentity")["status"] == "missing_permissions"


@pytest.mark.parametrize("missing", sorted(BLUEPRINT_ROLES))
def test_blueprint_child_permissions_must_be_on_blueprint_principal_not_mi(missing):
    graph, directory = graph_client(caller_permissions=MI_ROLES | BLUEPRINT_ROLES)
    directory.grants[MI] = directory.assignments(MI, MI_ROLES | BLUEPRINT_ROLES)
    directory.grants[BLUEPRINT_PRINCIPAL] = directory.assignments(BLUEPRINT_PRINCIPAL, BLUEPRINT_ROLES - {missing})
    report = run(graph)
    assert component(report, "configurationManagedIdentity")["status"] == "satisfied"
    blueprint = component(report, "blueprintPrincipal")
    assert blueprint["status"] == "missing_permissions" and missing in blueprint["missingPermissions"]
    assert report["deploymentReady"] is False


def test_assignment_to_another_resource_is_not_a_graph_role():
    graph, directory = graph_client()
    directory.grants[BLUEPRINT_PRINCIPAL] = directory.assignments(BLUEPRINT_PRINCIPAL, BLUEPRINT_ROLES, resource=CLIENT)
    report = run(graph)
    assert component(report, "blueprintPrincipal")["missingPermissions"] == sorted(BLUEPRINT_ROLES)
    assert not report["deploymentReady"]


def test_disabled_graph_role_does_not_count_as_granted():
    graph, directory = graph_client()
    for role in directory.catalog["appRoles"]:
        if role["value"] == "AgentIdentity.CreateAsManager":
            role["isEnabled"] = False
    report = run(graph)
    assert not report["deploymentReady"]
    assert "AgentIdentity.CreateAsManager" in component(report, "blueprintPrincipal")["missingPermissions"]


def test_nonapplication_graph_role_cannot_satisfy_mi_permission():
    graph, directory = graph_client()
    for role in directory.catalog["appRoles"]:
        if role["value"] == "AgentIdentityBlueprint.UpdateAuthProperties.All":
            role["allowedMemberTypes"] = ["User"]
    assert run(graph)["deploymentReady"] is False


def test_unknown_assigned_graph_role_is_inconclusive_not_inferred():
    graph, directory = graph_client()
    directory.grants[MI][0]["appRoleId"] = str(UUID(int=123456))
    report = run(graph)
    assert report["status"] == "inconclusive" and report["deploymentReady"] is False
    assert component(report, "configurationManagedIdentity")["error"]["code"] == "inconclusive_directory"


@pytest.mark.parametrize("path", [
    GRAPH_CATALOG_PATH, BLUEPRINT_PATH, PRINCIPALS_PATH, FIC_PATH,
    f"/v1.0/servicePrincipals/{MI}", f"/v1.0/servicePrincipals/{MI}/appRoleAssignments",
    f"/v1.0/servicePrincipals/{BLUEPRINT_PRINCIPAL}/appRoleAssignments",
])
def test_directory_access_denied_is_inconclusive_and_never_satisfied(path):
    graph, directory = graph_client()
    directory.overrides[path] = [Response({"error": {"message": "SECRET bearer/token data"}}, status=403)]
    report = run(graph)
    assert report["status"] == "inconclusive" and not report["deploymentReady"]
    assert "SECRET" not in json.dumps(report)
    failed = [c for c in report["checks"] if c["status"] == "inconclusive"]
    assert failed and any(c.get("error", {}).get("httpStatus") == 403 for c in failed)


@pytest.mark.parametrize("limited", [{}, {"value": None}, {"value": [{"appRoleId": "hidden"}]}])
def test_limited_assignment_information_blocks_instead_of_passing(limited):
    graph, directory = graph_client()
    directory.overrides[f"/v1.0/servicePrincipals/{MI}/appRoleAssignments"] = [Response(limited)]
    report = run(graph)
    assert report["status"] == "inconclusive" and not report["deploymentReady"]


def test_assignment_for_wrong_principal_blocks_even_with_matching_role_ids():
    graph, directory = graph_client()
    directory.grants[MI] = directory.assignments(CALLER, MI_ROLES)
    report = run(graph)
    assert report["status"] == "inconclusive" and not report["deploymentReady"]


def test_missing_role_catalog_is_inconclusive_not_an_empty_permission_set():
    graph, directory = graph_client()
    directory.catalog["appRoles"] = None
    report = run(graph)
    assert report["status"] == "inconclusive"
    assert component(report, "configurationManagedIdentity")["status"] == "inconclusive"


def test_missing_tenant_role_definitions_are_reported_as_unavailable():
    graph, directory = graph_client()
    directory.catalog["appRoles"] = []
    directory.grants[MI] = []
    report = preflight.identity_preflight(graph, managed_identity_principal_id=MI, stage="bootstrap")
    assert report["status"] == "inconclusive" and not report["deploymentReady"]
    assert all(c["status"] == "inconclusive" for c in component(report, "configurationManagedIdentity")["permissionChecks"])


def test_configuration_must_be_real_enabled_managed_identity():
    graph, directory = graph_client()
    directory.mi["servicePrincipalType"] = "Application"
    assert run(graph)["deploymentReady"] is False
    directory.mi["servicePrincipalType"] = "ManagedIdentity"
    directory.mi["accountEnabled"] = False
    report = run(graph)
    assert not report["deploymentReady"]
    assert component(report, "configurationManagedIdentity")["status"] == "missing_prerequisite"


def test_adopted_blueprint_and_named_fic_do_not_require_unused_create_roles():
    graph, directory = graph_client()
    directory.grants[MI] = directory.assignments(MI, MI_ROLES - {"AgentIdentityBlueprint.Create", "AgentIdentityBlueprintPrincipal.Create"})
    report = run(graph)
    assert report["deploymentReady"] is True
    operations = component(report, "configurationManagedIdentity")["permissionChecks"]
    assert not any(c["operation"].endswith("Create") for c in operations)


def test_missing_fic_requires_create_even_when_blueprint_is_precreated():
    graph, directory = graph_client()
    directory.fics = []
    directory.grants[MI] = directory.assignments(MI, MI_ROLES - {"AgentIdentityBlueprint.Create"})
    report = run(graph)
    assert not report["deploymentReady"]
    check = component(report, "configurationManagedIdentity")
    assert "AgentIdentityBlueprint.Create" in check["missingPermissions"]
    assert check["federation"] == "deploymentWillReconcile"


def test_missing_blueprint_principal_blocks_full_deployment_until_external_consent():
    graph, directory = graph_client()
    directory.principals = []
    report = run(graph)
    assert report["status"] == "blocked" and not report["deploymentReady"]
    assert component(report, "blueprintPrincipal")["status"] == "missing_prerequisite"
    assert not any(f"/{BLUEPRINT_PRINCIPAL}/appRoleAssignments" in url for _, url in directory.calls)


def test_blueprint_principal_and_fic_ambiguity_are_blockers():
    graph, directory = graph_client()
    directory.principals.append(deepcopy(directory.principals[0]))
    assert run(graph)["status"] == "inconclusive"
    directory.principals.pop()
    directory.fics.append(deepcopy(directory.fics[0]))
    assert run(graph)["status"] == "inconclusive"


def test_fic_drift_is_reported_but_is_not_misrepresented_as_an_exchange_test():
    graph, directory = graph_client()
    directory.fics[0]["subject"] = CLIENT
    report = run(graph)
    assert report["deploymentReady"] is True
    assert component(report, "configurationManagedIdentity")["federation"] == "deploymentWillReconcile"
    assert any("not MI-to-blueprint token issuance" in text for text in report["limitations"])
    assert all(method == "GET" for method, _ in directory.calls)


def test_adopting_precreated_agent_accepts_read_only_blueprint_grant_and_validates_parent():
    graph, directory = graph_client()
    directory.grants[BLUEPRINT_PRINCIPAL] = directory.assignments(BLUEPRINT_PRINCIPAL, {"AgentIdentity.Read.All"})
    report = run(graph, agent_identity_id=AGENT)
    assert report["deploymentReady"] is True
    assert component(report, "precreatedAgent")["agentIdentityId"] == AGENT
    assert component(report, "blueprintPrincipal")["permissionChecks"][0]["operation"] == "readPrecreatedAgent"
    # appId can be omitted for ServiceIdentity principals; id IS the client ID.
    assert "appId" not in directory.agent
    directory.agent["agentIdentityBlueprintId"] = CLIENT
    assert run(graph, agent_identity_id=AGENT)["deploymentReady"] is False


@pytest.mark.parametrize("change", [{"accountEnabled": False}, {"appId": CLIENT}, {"disabledByMicrosoftStatus": "Disabled"}])
def test_precreated_agent_disabled_or_mismatched_never_gets_implicitly_enabled(change):
    graph, directory = graph_client()
    directory.agent.update(change)
    report = run(graph, agent_identity_id=AGENT)
    assert not report["deploymentReady"]
    assert all(method == "GET" for method, _ in directory.calls)


def test_role_assignment_pagination_is_exhausted_before_permission_decision():
    graph, directory = graph_client()
    path = f"/v1.0/servicePrincipals/{MI}/appRoleAssignments"
    grants = directory.grants[MI]
    next_link = registry.GRAPH_ORIGIN + path + "?$skiptoken=page2"
    directory.overrides[path] = [
        Response({"value": grants[:1], "@odata.nextLink": next_link}),
        Response({"value": grants[1:]}),
    ]
    report = run(graph)
    assert report["deploymentReady"] is True
    assert any(url == next_link for _, url in directory.calls)


def test_unsafe_role_assignment_nextlink_blocks_even_if_first_page_had_all_roles():
    graph, directory = graph_client()
    path = f"/v1.0/servicePrincipals/{MI}/appRoleAssignments"
    directory.overrides[path] = [Response({"value": directory.grants[MI], "@odata.nextLink": "https://graph.microsoft.com.evil.example/SECRET"})]
    report = run(graph)
    assert report["status"] == "inconclusive" and not report["deploymentReady"]
    assert "SECRET" not in json.dumps(report)
    assert not any("evil.example" in url for _, url in directory.calls)


def test_registry_opt_in_checks_different_permission_set_without_mi_inference():
    graph, _ = graph_client(caller_permissions={"User.Read"})
    report = run(graph, registry=True)
    assert component(report, "configurationManagedIdentity")["status"] == "satisfied"
    assert component(report, "blueprintPrincipal")["status"] == "satisfied"
    registry_check = component(report, "registryPublisher")
    assert registry_check["status"] == "missing_permissions"
    assert set(registry_check["missingPermissions"]) == PUBLISHER_ROLES
    assert not report["deploymentReady"]


def test_optional_registry_check_can_pass_independently_of_identity_scope_claims():
    graph, _ = graph_client()
    report = run(graph, registry=True)
    assert report["deploymentReady"] is True
    check = component(report, "registryPublisher")
    assert check["status"] == "satisfied" and check["principalId"] == CALLER
    assert check["endpointCheck"] == "notAvailableWithoutSavedId"


def test_opaque_operator_token_does_not_invalidate_actual_mi_assignment_reads():
    graph, _ = graph_client()
    graph.credential.get_token.return_value = AccessToken("opaque-SECRET", 5000)
    report = run(graph)
    assert report["deploymentReady"] is True
    assert "SECRET" not in json.dumps(report)
    report = run(graph, registry=True)
    assert report["status"] == "inconclusive" and not report["deploymentReady"]


@pytest.mark.parametrize("stage,missing,expected", [("deploy", False, 0), ("deploy", True, 2), ("bootstrap", False, 0)])
def test_cli_stage_contract_and_nonzero_missing_permissions(stage, missing, expected, monkeypatch, capsys):
    graph, directory = graph_client()
    if missing:
        directory.grants[BLUEPRINT_PRINCIPAL] = []
    monkeypatch.setattr(preflight, "GraphClient", lambda **kwargs: graph)
    args = ["--managed-identity-principal-id", MI, "--stage", stage]
    if stage == "deploy":
        args += ["--blueprint-app-id", BLUEPRINT]
    assert preflight.main(args) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["deploymentReady"] == (stage == "deploy" and expected == 0)
    assert "signature" not in json.dumps(report)


def test_cli_directory_denial_exits_inconclusive_and_redacts_error(monkeypatch, capsys):
    graph, directory = graph_client()
    directory.overrides[GRAPH_CATALOG_PATH] = [Response({"error": "SECRET token echo"}, status=403)]
    monkeypatch.setattr(preflight, "GraphClient", lambda **kwargs: graph)
    assert preflight.main(["--managed-identity-principal-id", MI]) == 3
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert json.loads(output)["deploymentReady"] is False


def test_cli_invalid_ids_and_arguments_never_echo_values(monkeypatch, capsys):
    graph, directory = graph_client()
    monkeypatch.setattr(preflight, "GraphClient", lambda **kwargs: graph)
    assert preflight.main(["--managed-identity-principal-id", "SECRET wrong ID"]) == 2
    assert "SECRET" not in capsys.readouterr().out
    assert preflight.main(["--unknown", "SECRET argument"]) == 2
    assert "SECRET" not in capsys.readouterr().out
    assert not directory.calls


def test_no_wildcard_permission_or_grant_endpoint_is_ever_used():
    graph, directory = graph_client()
    run(graph, agent_identity_id=AGENT, registry=True)
    for method, url in directory.calls:
        assert method == "GET"
        assert "/oauth2PermissionGrants" not in url
        assert "/appRoleAssignedTo" not in url
        assert "/roleManagement/" not in url


def test_cli_opted_in_deployment_cannot_omit_registry_permission_check(monkeypatch, capsys):
    graph, _ = graph_client(caller_permissions={"User.Read"})
    monkeypatch.setattr(preflight, "GraphClient", lambda **kwargs: graph)
    monkeypatch.setenv("AGENT_REGISTRY_ENABLED", "true")
    assert preflight.main(["--managed-identity-principal-id", MI, "--blueprint-app-id", BLUEPRINT]) == 2
    report = json.loads(capsys.readouterr().out)
    assert component(report, "registryPublisher")["status"] == "missing_permissions"
    assert not report["deploymentReady"]


def test_cli_invalid_registry_opt_in_fails_closed_without_authentication(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_REGISTRY_ENABLED", "SECRET misspelling")
    assert preflight.main([]) == 2
    assert "SECRET" not in capsys.readouterr().out
    registry.AzureCliCredential.assert_not_called()


def test_limited_principal_account_state_is_inconclusive():
    graph, directory = graph_client()
    del directory.mi["accountEnabled"]
    report = run(graph)
    assert component(report, "configurationManagedIdentity")["status"] == "inconclusive"
    assert report["deploymentReady"] is False