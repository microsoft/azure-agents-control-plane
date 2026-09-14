"""Adopt completed Agent 365 CLI onboarding into a named azd environment.

The Agent 365 CLI state can contain a protected client secret. This helper
prepares UAMI-backed CLI state with --prepare, then reads only allowlisted,
non-secret fields for adoption. It never prints the source document or copies
the CLI-managed registration ID into the repository's different publisher.
Without --prepare or --apply it performs live validation only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlencode
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_CONFIG_BYTES = 1024 * 1024
OBSERVABILITY_APP_ID = "9b975845-388f-4429-889e-eab1ef63949c"
OBSERVABILITY_ROLE = "Agent365.Observability.OtelWrite"
MANAGED_CLI_APP_ID = "f54280f4-395e-4ea8-9e48-bf2d4952aa14"
DEFAULT_OBSERVABILITY_SKUS = ("Microsoft_Agent_365_Tier3", "MICROSOFT_365_E7")
VERIFICATION_SCOPES = (
    "https://graph.microsoft.com/AgentIdentityBlueprint.ReadWrite.All",
    "https://graph.microsoft.com/AgentIdentity.Read.All",
    "https://graph.microsoft.com/AgentRegistration.ReadWrite.All",
    "https://graph.microsoft.com/Application.Read.All",
)


class AdoptionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class VerificationCredential:
    """Request the Agent 365 CLI's consented scopes, never Azure CLI's scopes."""

    def __init__(self, tenant_id: str, client_app_id: str, *, credential=None):
        self.tenant_id = _guid(tenant_id, "Verification tenant ID")
        self.client_app_id = _guid(client_app_id, "Agent 365 CLI application ID")
        if credential is None:
            try:
                from azure.identity import InteractiveBrowserCredential  # noqa: PLC0415

                credential = InteractiveBrowserCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_app_id,
                    redirect_uri="http://localhost:8400/",
                )
            except Exception:
                raise AdoptionError("verification_auth_unavailable", "Interactive Agent 365 verification authentication is unavailable.") from None
        self.credential = credential

    def get_token(self, *requested_scopes, **kwargs):
        try:
            return self.credential.get_token(*VERIFICATION_SCOPES, **kwargs)
        except Exception:
            raise AdoptionError(
                "verification_auth_failed",
                "Cannot acquire the consented Agent 365 CLI verification token. Sign in as the required owner and confirm client-app admin consent.",
            ) from None

    def close(self):
        close = getattr(self.credential, "close", None)
        if close:
            close()


def _guid(value: str, label: str) -> str:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
            raise ValueError
        parsed = UUID(value)
        if not parsed.int:
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError):
        raise AdoptionError("invalid_configuration", label + " must be a nonzero GUID.") from None


def _read_object(path: Path, label: str) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise AdoptionError("invalid_configuration", f"{label} is missing or invalid; content suppressed.") from None


def _read_optional_object(path: Path, label: str) -> dict:
    return _read_object(path, label) if path.exists() else {}


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise AdoptionError("unsafe_path", "Agent 365 configuration path must not be a symbolic link.")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AdoptionError("configuration_write_failed", "Agent 365 configuration could not be written.") from None


def prepared_configuration(
    static: dict, generated: dict, *, target: dict[str, str], client_app_id: str,
    agent_name: str,
) -> tuple[dict, dict]:
    if not isinstance(agent_name, str) or not agent_name.strip() or len(agent_name) > 128 or any(ord(c) < 32 for c in agent_name):
        raise AdoptionError("invalid_configuration", "Agent name is missing or invalid.")
    name = agent_name.strip()
    desired = {
        "tenantId": _guid(target["tenant"], "Target tenant ID"),
        "clientAppId": _guid(client_app_id, "Agent 365 CLI application ID"),
        "authMode": "s2s",
        "agentIdentityDisplayName": name + " Agent",
        "agentBlueprintDisplayName": name + " Blueprint",
        "agentDescription": name,
        "aiTeammate": False,
        "useBlueprint": True,
    }
    for key, expected in desired.items():
        if key in static and static[key] != expected:
            raise AdoptionError("configuration_conflict", "Existing Agent 365 static configuration conflicts with the requested installation.")
    if any(static.get(key) for key in ("deploymentProjectPath", "messagingEndpoint", "customBlueprintPermissions", "mcpDefaultServers", "needAzureOpenAI")):
        raise AdoptionError("configuration_conflict", "Existing Agent 365 configuration requests capabilities outside this installation.")
    prepared_static = {**static, **desired}

    principal = _guid(target["principalId"], "Bootstrap UAMI principal ID")
    existing_principal = generated.get("managedIdentityPrincipalId")
    if existing_principal and _guid(str(existing_principal), "Existing Agent 365 managed identity principal ID") != principal:
        raise AdoptionError("identity_mismatch", "Existing Agent 365 state is tied to a different managed identity.")
    prepared_generated = {**generated, "managedIdentityPrincipalId": principal}
    return prepared_static, prepared_generated


def resolve_owner(run_command, *, tenant_id: str, owner_upn: str) -> str:
    _guid(tenant_id, "Target tenant ID")
    if not isinstance(owner_upn, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+", owner_upn.strip()):
        raise AdoptionError("invalid_configuration", "Owner must be a valid user principal name.")
    try:
        current = json.loads(run_command([
            "az", "ad", "signed-in-user", "show", "--output", "json", "--only-show-errors",
        ]))
    except Exception:
        raise AdoptionError("owner_unverified", "Cannot resolve the signed-in Azure CLI user in the target tenant.") from None
    if not isinstance(current, dict):
        raise AdoptionError("owner_unverified", "Azure CLI returned an invalid signed-in user.")
    owner = _guid(str(current.get("id", "")), "Signed-in owner object ID")
    names = {str(current.get(key, "")).strip().casefold() for key in ("userPrincipalName", "mail")}
    if owner_upn.strip().casefold() not in names:
        raise AdoptionError("owner_mismatch", "The signed-in Azure CLI user is not the required Agent 365 owner.")
    return owner


def resolve_client_app(run_command, explicit: str | None = None) -> str:
    def available(candidate: str) -> bool:
        try:
            actual = run_command([
                "az", "ad", "sp", "show", "--id", candidate,
                "--query", "appId", "--output", "tsv", "--only-show-errors",
            ]).strip()
            return _guid(actual, "Agent 365 CLI service principal application ID") == candidate
        except Exception:
            return False

    if explicit:
        candidate = _guid(explicit, "Agent 365 CLI application ID")
        if available(candidate):
            return candidate
    elif available(MANAGED_CLI_APP_ID):
        return MANAGED_CLI_APP_ID
    else:
        try:
            applications = json.loads(run_command([
                "az", "ad", "app", "list", "--display-name", "Agent 365 CLI",
                "--query", "[].appId", "--output", "json", "--only-show-errors",
            ]))
        except Exception:
            applications = []
        candidates = []
        if isinstance(applications, list):
            for value in applications:
                try:
                    candidate = _guid(value, "Tenant Agent 365 CLI application ID")
                    if available(candidate):
                        candidates.append(candidate)
                except AdoptionError:
                    continue
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise AdoptionError("client_app_ambiguous", "Multiple tenant-owned Agent 365 CLI applications are available; pass --client-app-id explicitly.")
    raise AdoptionError(
        "client_app_unavailable",
        "No approved Agent 365 CLI enterprise application is available. Run 'a365 setup requirements' as Global Administrator, then retry.",
    )


def adoption_values(
    static: dict, generated: dict, *, target: dict[str, str], owner_id: str,
) -> dict[str, str]:
    tenant = _guid(str(static.get("tenantId", "")), "Agent 365 tenant ID")
    if tenant != target["tenant"]:
        raise AdoptionError("tenant_mismatch", "Agent 365 state belongs to a different tenant.")
    if static.get("aiTeammate") is not False or static.get("useBlueprint") is not True:
        raise AdoptionError("invalid_configuration", "Agent 365 setup must use the standard blueprint-agent path.")
    if str(static.get("authMode", "")).strip().lower() != "s2s":
        raise AdoptionError("invalid_configuration", "Agent 365 authMode must be s2s for the autonomous AKS workload.")
    _guid(str(static.get("clientAppId", "")), "Agent 365 CLI application ID")
    display_name = static.get("agentIdentityDisplayName")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 256:
        raise AdoptionError("invalid_configuration", "Agent 365 identity display name is missing or invalid.")
    if generated.get("completed") is not True:
        raise AdoptionError("onboarding_incomplete", "Agent 365 setup is incomplete; finish administrator consent and S2S grants before adoption.")

    managed_identity = _guid(str(generated.get("managedIdentityPrincipalId", "")), "Agent 365 managed identity principal ID")
    if managed_identity != target["principalId"]:
        raise AdoptionError("identity_mismatch", "Agent 365 blueprint federation targets a different managed identity.")

    blueprint = _guid(str(generated.get("agentBlueprintId", "")), "Agent 365 blueprint client ID")
    blueprint_object = _guid(str(generated.get("agentBlueprintObjectId", "")), "Agent 365 blueprint object ID")
    blueprint_principal = _guid(
        str(generated.get("agentBlueprintServicePrincipalObjectId", "")),
        "Agent 365 blueprint principal ID",
    )
    agent = _guid(str(generated.get("agenticAppId", "")), "Agent 365 agent identity ID")
    if len({blueprint, blueprint_object, blueprint_principal, agent, managed_identity}) != 5:
        raise AdoptionError("identity_mismatch", "Agent 365 identity, blueprint, and UAMI identifiers must be distinct.")

    owner = _guid(owner_id, "Agent 365 owner object ID")
    return {
        "AGENT_IDENTITY_PROVISIONING_MODE": "adopt",
        "AGENT_IDENTITY_ENABLED": "false",
        "AGENT_IDENTITY_APP_ID": agent,
        "AGENT_IDENTITY_PRINCIPAL_ID": agent,
        "AGENT_IDENTITY_BLUEPRINT_APP_ID": blueprint,
        "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID": blueprint_object,
        "AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID": blueprint_principal,
        "AGENT_IDENTITY_DISPLAY_NAME": display_name.strip(),
        "EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID": blueprint,
        "EXISTING_AGENT_IDENTITY_ID": agent,
        "AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID": target["resourceId"],
        "AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID": target["principalId"],
        "AGENT_IDENTITY_CONFIGURATION_NAME": target["name"],
        "AGENT_SPONSOR_PRINCIPAL_ID": owner,
        "AGENT_REGISTRY_OWNER_IDS": owner,
        "AGENT_REGISTRY_ENABLED": "false",
        "AGENT_OBSERVABILITY_MODE": "agent365",
    }


def _registration_id(value: object) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 512
        or value in (".", "..") or any(c in value for c in "/\\?#%") or any(c.isspace() for c in value)
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise AdoptionError("invalid_configuration", "Agent 365 registration ID is missing or invalid.")
    return value


def verify_observability_license(
    graph, eligible_license_skus: tuple[str, ...] = DEFAULT_OBSERVABILITY_SKUS,
) -> dict:
    eligible = {sku.strip().casefold() for sku in eligible_license_skus if isinstance(sku, str) and sku.strip()}
    if not eligible:
        raise AdoptionError("invalid_configuration", "At least one eligible observability license SKU must be configured.")
    subscriptions = graph.collection("/v1.0/subscribedSkus?$select=skuPartNumber,consumedUnits,capabilityStatus")
    assigned = [
        sku for sku in subscriptions
        if str(sku.get("skuPartNumber", "")).casefold() in eligible
        and isinstance(sku.get("consumedUnits"), int) and sku["consumedUnits"] > 0
        and sku.get("capabilityStatus") in ("Enabled", "Warning")
    ]
    if not assigned:
        raise AdoptionError("observability_license_missing", "No user has an assigned eligible Agent 365 observability license.")
    return {
        "component": "agent365ObservabilityLicense", "status": "satisfied",
        "skuPartNumber": assigned[0]["skuPartNumber"],
    }


def verify_agent365_resources(
    graph, generated: dict, *, owner_id: str, client_app_id: str | None = None,
) -> dict:
    owner = _guid(owner_id, "Agent 365 owner object ID")
    blueprint_object = _guid(str(generated.get("agentBlueprintObjectId", "")), "Agent 365 blueprint object ID")
    agent = _guid(str(generated.get("agenticAppId", "")), "Agent 365 agent identity ID")
    registration_id = _registration_id(generated.get("agentRegistrationId"))
    caller = graph.caller()
    if caller is None or caller.get("principalId") != owner:
        raise AdoptionError("owner_mismatch", "Agent 365 verification must authenticate as the intended owner.")
    if client_app_id and caller.get("clientId") != _guid(client_app_id, "Agent 365 CLI application ID"):
        raise AdoptionError("client_app_mismatch", "Agent 365 verification used a different client application.")

    owner_checks = (
        ("blueprintOwner", f"/v1.0/applications/{blueprint_object}/owners?$select=id"),
        ("agentIdentityOwner", f"/v1.0/servicePrincipals/{agent}/owners?$select=id"),
    )
    checks = []
    for component, path in owner_checks:
        owners = graph.collection(path)
        owner_ids = {_guid(str(item.get("id", "")), "Returned owner object ID") for item in owners}
        if owner not in owner_ids:
            raise AdoptionError("owner_mismatch", f"The intended user is not the verified {component}.")
        checks.append({"component": component, "status": "satisfied", "ownerId": owner})

    registration = graph.request("GET", f"/beta/copilot/agentRegistrations/{registration_id}")
    if registration.get("id") != registration_id or agent != registration.get("agentIdentityId"):
        raise AdoptionError("registration_mismatch", "Agent 365 registration is not bound to the generated Agent ID.")
    registration_owners = registration.get("ownerIds")
    if not isinstance(registration_owners, list) or owner not in {
        _guid(str(item), "Registration owner object ID") for item in registration_owners
    }:
        raise AdoptionError("owner_mismatch", "The intended user is not an owner of the Agent 365 registration.")
    checks.append({"component": "agentRegistrationOwner", "status": "satisfied", "ownerId": owner})

    resource_query = urlencode({"$filter": f"appId eq '{OBSERVABILITY_APP_ID}'", "$select": "id,appId,appRoles"})
    resources = graph.collection("/v1.0/servicePrincipals?" + resource_query)
    if len(resources) != 1 or resources[0].get("appId") != OBSERVABILITY_APP_ID:
        raise AdoptionError("observability_unavailable", "Agent 365 observability service principal is missing or ambiguous.")
    roles = resources[0].get("appRoles")
    matching_roles = [
        role for role in roles if isinstance(role, dict) and role.get("value") == OBSERVABILITY_ROLE
        and role.get("isEnabled") is True and "Application" in role.get("allowedMemberTypes", [])
    ] if isinstance(roles, list) else []
    if len(matching_roles) != 1:
        raise AdoptionError("observability_unavailable", "Agent 365 observability application role is missing or ambiguous.")
    role_id = _guid(str(matching_roles[0].get("id", "")), "Agent 365 observability role ID")
    resource_id = _guid(str(resources[0].get("id", "")), "Agent 365 observability service principal ID")
    assignments = graph.collection(f"/v1.0/servicePrincipals/{agent}/appRoleAssignments?$select=appRoleId,principalId,resourceId")
    matching_assignments = [
        item for item in assignments
        if item.get("appRoleId") == role_id and item.get("principalId") == agent and item.get("resourceId") == resource_id
    ]
    if len(matching_assignments) != 1:
        raise AdoptionError("observability_consent_missing", "Agent ID is missing the Agent365.Observability.OtelWrite application role.")
    checks.append({"component": "agent365ObservabilityS2S", "status": "satisfied", "role": OBSERVABILITY_ROLE})
    return {"status": "satisfied", "checks": checks}


def main(argv: list[str] | None = None) -> int:
    try:
        from scripts.agent_identity_preflight import adopted_identity_preflight  # noqa: PLC0415
        from scripts.deployment_gate import (  # noqa: PLC0415
            GraphClient, SafeParser, load_configuration, resolve_target, run_cli,
        )

        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--environment", required=True)
        parser.add_argument("--owner-upn", default="christava@microsoft.com")
        parser.add_argument("--client-app-id")
        parser.add_argument("--agent-name", default="Next Best Action")
        parser.add_argument("--eligible-license-sku", action="append")
        parser.add_argument("--config-dir", type=Path, default=ROOT)
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--prepare", action="store_true")
        action.add_argument("--apply", action="store_true")
        args = parser.parse_args(argv)

        values = load_configuration(from_azd=True, environment=args.environment)
        target = resolve_target(values)
        owner_id = resolve_owner(run_cli, tenant_id=target["tenant"], owner_upn=args.owner_upn)
        eligible_license_skus = tuple(args.eligible_license_sku or DEFAULT_OBSERVABILITY_SKUS)
        static_path = args.config_dir / "a365.config.json"
        generated_path = args.config_dir / "a365.generated.config.json"
        if args.prepare:
            existing_static = _read_optional_object(static_path, "Agent 365 static configuration")
            client_app_id = resolve_client_app(run_cli, args.client_app_id or existing_static.get("clientAppId"))
            static, generated = prepared_configuration(
                existing_static,
                _read_optional_object(generated_path, "Agent 365 generated state"),
                target=target, client_app_id=client_app_id, agent_name=args.agent_name,
            )
            with GraphClient(tenant_id=target["tenant"]) as graph:
                license_check = verify_observability_license(graph, eligible_license_skus)
            _atomic_json(static_path, static)
            _atomic_json(generated_path, generated)
            print(json.dumps({
                "status": "prepared", "environment": args.environment,
                "ownerId": owner_id, "managedIdentityPrincipalId": target["principalId"],
                "configDirectory": str(args.config_dir.resolve()), "secretsPrinted": False,
                "checks": [license_check],
                "nextStep": "Run a365 setup all, then rerun this helper with --apply.",
            }, sort_keys=True))
            return 0
        static = _read_object(static_path, "Agent 365 static configuration")
        generated = _read_object(generated_path, "Agent 365 generated state")
        settings = adoption_values(static, generated, target=target, owner_id=owner_id)
        with GraphClient(tenant_id=target["tenant"]) as license_graph:
            license_check = verify_observability_license(license_graph, eligible_license_skus)
        credential = VerificationCredential(target["tenant"], static["clientAppId"])
        try:
            with GraphClient(tenant_id=target["tenant"], credential=credential) as graph:
                identity = adopted_identity_preflight(
                    graph,
                    managed_identity_principal_id=target["principalId"],
                    blueprint_app_id=settings["AGENT_IDENTITY_BLUEPRINT_APP_ID"],
                    agent_identity_id=settings["AGENT_IDENTITY_APP_ID"],
                )
                if identity.get("status") != "satisfied" or identity.get("deploymentReady") is not True:
                    raise AdoptionError("identity_unverified", "Agent ID relationship or UAMI federation could not be verified.")
                resources = verify_agent365_resources(
                    graph, generated, owner_id=owner_id, client_app_id=static["clientAppId"],
                )
        finally:
            credential.close()
        if args.apply:
            for name, value in sorted(settings.items()):
                run_cli(["azd", "env", "set", name, value, "--environment", args.environment])
        print(json.dumps({
            "status": "configured" if args.apply else "satisfied",
            "environment": args.environment,
            "readOnly": not args.apply,
            "settings": sorted(settings),
            "registryManagedBy": "Agent 365 CLI",
            "secretsImported": False,
            "checks": [*identity.get("checks", []), *resources["checks"], license_check],
        }, sort_keys=True))
        return 0
    except AdoptionError as error:
        print(json.dumps({"status": "blocked", "error": error.as_dict()}, sort_keys=True))
        return 2
    except Exception as error:
        if hasattr(error, "as_dict"):
            print(json.dumps({"status": "blocked", "error": error.as_dict()}, sort_keys=True))
            return 2
        print(json.dumps({"status": "inconclusive", "error": "Agent 365 adoption failed; source content suppressed."}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())