from pathlib import Path

import pytest

from scripts import adopt_agent365_onboarding as adoption


TENANT = "11111111-1111-1111-1111-111111111111"
UAMI = "22222222-2222-2222-2222-222222222222"
BLUEPRINT = "33333333-3333-3333-3333-333333333333"
BLUEPRINT_OBJECT = "44444444-4444-4444-4444-444444444444"
BLUEPRINT_PRINCIPAL = "55555555-5555-5555-5555-555555555555"
AGENT = "66666666-6666-6666-6666-666666666666"
OWNER = "77777777-7777-7777-7777-777777777777"
REGISTRATION = "88888888-8888-8888-8888-888888888888"
OBSERVABILITY_SP = "99999999-9999-9999-9999-999999999999"
OBSERVABILITY_ROLE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture
def state():
    static = {
        "tenantId": TENANT,
        "clientAppId": adoption.MANAGED_CLI_APP_ID,
        "agentIdentityDisplayName": "Next Best Action Agent",
        "authMode": "s2s",
        "aiTeammate": False,
        "useBlueprint": True,
    }
    generated = {
        "completed": True,
        "managedIdentityPrincipalId": UAMI,
        "agentBlueprintId": BLUEPRINT,
        "agentBlueprintObjectId": BLUEPRINT_OBJECT,
        "agentBlueprintServicePrincipalObjectId": BLUEPRINT_PRINCIPAL,
        "agenticAppId": AGENT,
        "agentRegistrationId": REGISTRATION,
        "agentBlueprintClientSecret": "SECRET-do-not-import",
    }
    target = {
        "tenant": TENANT,
        "principalId": UAMI,
        "resourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mcp-test",
        "name": "id-mcp-test",
    }
    return static, generated, target


def test_completed_s2s_state_maps_only_adoption_settings(state):
    static, generated, target = state
    values = adoption.adoption_values(static, generated, target=target, owner_id=OWNER)

    assert values["AGENT_IDENTITY_APP_ID"] == AGENT
    assert values["AGENT_IDENTITY_BLUEPRINT_APP_ID"] == BLUEPRINT
    assert values["AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID"] == UAMI
    assert values["AGENT_OBSERVABILITY_MODE"] == "agent365"
    assert values["AGENT_REGISTRY_ENABLED"] == "false"
    assert values["AGENT_IDENTITY_ENABLED"] == "false"
    assert "AGENT_REGISTRY_ID" not in values
    assert all("SECRET" not in value for value in values.values())


def test_preparation_seeds_existing_uami_and_preserves_generated_state(state):
    _, _, target = state
    generated = {"agentBlueprintClientSecret": "SECRET-preserved"}
    static, prepared = adoption.prepared_configuration(
        {}, generated, target=target,
        client_app_id=adoption.MANAGED_CLI_APP_ID,
        agent_name="Next Best Action",
    )

    assert static == {
        "tenantId": TENANT,
        "clientAppId": adoption.MANAGED_CLI_APP_ID,
        "authMode": "s2s",
        "agentIdentityDisplayName": "Next Best Action Agent",
        "agentBlueprintDisplayName": "Next Best Action Blueprint",
        "agentDescription": "Next Best Action",
        "aiTeammate": False,
        "useBlueprint": True,
    }
    assert prepared["managedIdentityPrincipalId"] == UAMI
    assert prepared["agentBlueprintClientSecret"] == "SECRET-preserved"


def test_preparation_refuses_tenant_or_uami_retargeting(state):
    _, _, target = state
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.prepared_configuration(
            {"tenantId": OWNER}, {}, target=target,
            client_app_id=adoption.MANAGED_CLI_APP_ID,
            agent_name="Next Best Action",
        )
    assert caught.value.code == "configuration_conflict"

    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.prepared_configuration(
            {}, {"managedIdentityPrincipalId": OWNER}, target=target,
            client_app_id=adoption.MANAGED_CLI_APP_ID,
            agent_name="Next Best Action",
        )
    assert caught.value.code == "identity_mismatch"


@pytest.mark.parametrize("key,value", [
    ("deploymentProjectPath", "src"),
    ("messagingEndpoint", "https://example.test/messages"),
    ("customBlueprintPermissions", [{"resourceAppId": OWNER}]),
    ("mcpDefaultServers", [{"name": "mail"}]),
    ("needAzureOpenAI", True),
])
def test_preparation_refuses_unrequested_cli_capabilities(state, key, value):
    _, _, target = state
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.prepared_configuration(
            {key: value}, {}, target=target,
            client_app_id=adoption.MANAGED_CLI_APP_ID,
            agent_name="Next Best Action",
        )
    assert caught.value.code == "configuration_conflict"


def test_owner_resolution_requires_the_intended_signed_in_user():
    current = {"id": OWNER, "userPrincipalName": "christava@microsoft.com", "mail": "christava@microsoft.com"}
    run = lambda command: __import__("json").dumps(current)
    assert adoption.resolve_owner(run, tenant_id=TENANT, owner_upn="christava@microsoft.com") == OWNER

    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.resolve_owner(run, tenant_id=TENANT, owner_upn="someone@microsoft.com")
    assert caught.value.code == "owner_mismatch"


def test_client_app_resolution_prefers_available_managed_enterprise_app():
    def run(command):
        if command[2:4] == ["app", "list"]:
            return "[]"
        assert command[2:4] == ["sp", "show"]
        return adoption.MANAGED_CLI_APP_ID

    assert adoption.resolve_client_app(run) == adoption.MANAGED_CLI_APP_ID


def test_client_app_resolution_rejects_ambiguous_custom_apps():
    custom_one = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    custom_two = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    def run(command):
        if command[2:4] == ["app", "list"]:
            return __import__("json").dumps([custom_one, custom_two])
        if command[2:4] == ["sp", "show"] and command[5] in (custom_one, custom_two):
            return command[5]
        raise RuntimeError

    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.resolve_client_app(run)
    assert caught.value.code == "client_app_ambiguous"


@pytest.mark.parametrize(
    "section,key,value,code",
    [
        ("generated", "completed", False, "onboarding_incomplete"),
        ("generated", "managedIdentityPrincipalId", OWNER, "identity_mismatch"),
        ("static", "tenantId", OWNER, "tenant_mismatch"),
        ("static", "authMode", "obo", "invalid_configuration"),
        ("static", "aiTeammate", True, "invalid_configuration"),
    ],
)
def test_incomplete_or_wrong_onboarding_is_rejected(state, section, key, value, code):
    static, generated, target = state
    (static if section == "static" else generated)[key] = value

    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.adoption_values(static, generated, target=target, owner_id=OWNER)

    assert caught.value.code == code
    assert "SECRET" not in str(caught.value)


def test_identity_identifiers_must_be_distinct(state):
    static, generated, target = state
    generated["agenticAppId"] = BLUEPRINT

    with pytest.raises(adoption.AdoptionError, match="must be distinct"):
        adoption.adoption_values(static, generated, target=target, owner_id=OWNER)


class Graph:
    def __init__(self, *, missing_owner=None, missing_role=False, missing_license=False):
        self.missing_owner = missing_owner
        self.missing_role = missing_role
        self.missing_license = missing_license

    def collection(self, path):
        if "/applications/" in path and "/owners" in path:
            return [] if self.missing_owner == "blueprint" else [{"id": OWNER}]
        if "/owners" in path:
            return [] if self.missing_owner == "agent" else [{"id": OWNER}]
        if "servicePrincipals?" in path:
            return [{
                "id": OBSERVABILITY_SP,
                "appId": adoption.OBSERVABILITY_APP_ID,
                "appRoles": [{
                    "id": OBSERVABILITY_ROLE_ID,
                    "value": adoption.OBSERVABILITY_ROLE,
                    "isEnabled": True,
                    "allowedMemberTypes": ["Application"],
                }],
            }]
        if "/appRoleAssignments" in path:
            return [] if self.missing_role else [{
                "appRoleId": OBSERVABILITY_ROLE_ID,
                "principalId": AGENT,
                "resourceId": OBSERVABILITY_SP,
            }]
        if "/subscribedSkus" in path:
            return [{
                "skuPartNumber": "Microsoft_Agent_365_Tier3",
                "consumedUnits": 0 if self.missing_license else 1,
                "capabilityStatus": "Enabled",
            }]
        raise AssertionError(path)

    def request(self, method, path):
        assert method == "GET" and path.endswith(REGISTRATION)
        return {"id": REGISTRATION, "agentIdentityId": AGENT, "ownerIds": [OWNER]}

    def caller(self):
        return {"principalId": OWNER, "clientId": adoption.MANAGED_CLI_APP_ID}


def test_resource_verification_requires_owner_registration_and_s2s_role(state):
    _, generated, _ = state
    report = adoption.verify_agent365_resources(
        Graph(), generated, owner_id=OWNER, client_app_id=adoption.MANAGED_CLI_APP_ID,
    )

    assert report["status"] == "satisfied"
    assert {item["component"] for item in report["checks"]} == {
        "blueprintOwner", "agentIdentityOwner", "agentRegistrationOwner",
        "agent365ObservabilityS2S",
    }


@pytest.mark.parametrize("missing", ["blueprint", "agent"])
def test_resource_verification_rejects_missing_owner(state, missing):
    _, generated, _ = state
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_agent365_resources(Graph(missing_owner=missing), generated, owner_id=OWNER)
    assert caught.value.code == "owner_mismatch"


def test_resource_verification_rejects_missing_observability_role(state):
    _, generated, _ = state
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_agent365_resources(Graph(missing_role=True), generated, owner_id=OWNER)
    assert caught.value.code == "observability_consent_missing"


def test_resource_verification_rejects_unassigned_observability_license(state):
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_observability_license(Graph(missing_license=True))
    assert caught.value.code == "observability_license_missing"


def test_license_verification_accepts_only_assigned_eligible_sku():
    check = adoption.verify_observability_license(Graph())
    assert check["component"] == "agent365ObservabilityLicense"

    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_observability_license(Graph(), ("OTHER_SKU",))
    assert caught.value.code == "observability_license_missing"


def test_resource_verification_rejects_wrong_registration_owner(state):
    _, generated, _ = state
    graph = Graph()
    graph.request = lambda method, path: {"id": REGISTRATION, "agentIdentityId": AGENT, "ownerIds": [UAMI]}
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_agent365_resources(graph, generated, owner_id=OWNER)
    assert caught.value.code == "owner_mismatch"


def test_resource_verification_rejects_wrong_verification_caller(state):
    _, generated, _ = state
    graph = Graph()
    graph.caller = lambda: {"principalId": UAMI, "clientId": adoption.MANAGED_CLI_APP_ID}
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.verify_agent365_resources(graph, generated, owner_id=OWNER)
    assert caught.value.code == "owner_mismatch"


def test_verification_credential_requests_only_agent365_scopes():
    class Credential:
        def __init__(self):
            self.scopes = None
            self.closed = False

        def get_token(self, *scopes, **kwargs):
            self.scopes = scopes
            return "token"

        def close(self):
            self.closed = True

    inner = Credential()
    credential = adoption.VerificationCredential(TENANT, adoption.MANAGED_CLI_APP_ID, credential=inner)
    assert credential.get_token("https://graph.microsoft.com/.default") == "token"
    assert inner.scopes == adoption.VERIFICATION_SCOPES
    credential.close()
    assert inner.closed


def test_administrator_installer_uses_official_s2s_flow_and_guarded_adoption():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install-agent365.ps1").read_text(encoding="utf-8")

    assert "'setup', 'requirements'" in script
    assert "'setup', 'all', '--dry-run'" in script
    assert "'setup', 'all'" in script
    assert "--prepare" in script and "--apply" in script
    assert "scripts/deployment_gate.py" in script
    assert "'provision', '--environment'" in script
    assert "--m365" not in script
    assert "--skip-requirements" not in script