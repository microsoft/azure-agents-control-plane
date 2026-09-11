"""Stage-one identity deployment against an existing resource group and UAMI.

Default: check prerequisites only. --apply provisions only the scoped identity
template; it never runs azd provision/hooks, logs in, grants Graph consent, builds
an image or contacts Kubernetes/Teams. Blueprint bootstrap may stop for external
administrator consent before the identity phase. --import-outputs recovers the
same successful ARM deployment's verified outputs without replaying provisioning.
Only verified, nonempty identity metadata is saved to the named azd environment.
Do not automatically retry an ambiguous deployment; inspect/recover it first.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.deployment_gate import (  # noqa: E402
    GateBlocked, GraphClient, RegistryError, SafeParser, flag, identity_preflight,
    json_object, load_configuration, nonempty, nonzero_guid, resolve_target,
    run_cli, verify_report,
)

IDENTITY_OUTPUTS = (
    "AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID",
    "AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID", "AGENT_IDENTITY_APP_ID",
    "AGENT_IDENTITY_PRINCIPAL_ID", "AGENT_IDENTITY_DISPLAY_NAME",
    "AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID", "AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID",
    "MCP_SERVER_IDENTITY_CLIENT_ID",
)


def adoption_id(values: dict[str, str], input_key: str, output_key: str, override: str | None) -> str:
    ids = [nonzero_guid(value, input_key) for value in (override, values.get(input_key), values.get(output_key)) if value]
    if len(set(ids)) > 1:
        raise RegistryError("identity_mismatch", "Conflicting adoption and generated IDs: " + input_key + ".")
    return ids[0] if ids else ""


def parameters(values: dict[str, str], target: dict[str, str], phase: str, blueprint: str, agent: str) -> dict:
    sponsor = nonzero_guid(nonempty(values, "AGENT_SPONSOR_PRINCIPAL_ID"), "Sponsor user ID")
    # Match main.bicep's existing deployment names/tag so scoped/full paths can
    # adopt one another. The existing UAMI's suffix is that deployment's token.
    if not target["name"].startswith("id-mcp-") and not values.get("AGENT_BLUEPRINT_UNIQUE_NAME"):
        raise RegistryError("missing_configuration", "Custom UAMI names require AGENT_BLUEPRINT_UNIQUE_NAME.")
    suffix = target["name"].removeprefix("id-mcp-")
    result = {
        "phase": phase, "managedIdentityName": target["name"],
        "blueprintDisplayName": values.get("AGENT_BLUEPRINT_DISPLAY_NAME") or f"NextBestAction-Blueprint-{suffix}",
        "blueprintUniqueName": values.get("AGENT_BLUEPRINT_UNIQUE_NAME") or f"nba-blueprint-{suffix}",
        "agentDisplayName": values.get("AGENT_IDENTITY_DISPLAY_NAME") or f"NextBestAction-Agent-{suffix}",
        "sponsorPrincipalId": sponsor, "existingBlueprintAppId": blueprint,
        "existingAgentIdentityId": agent,
        "forceUpdateTag": str(uuid4()),
    }
    if phase == "identity":
        for parameter, key in (
            ("cosmosDbAccountName", "COSMOSDB_ACCOUNT_NAME"),
            ("storageAccountName", "AZURE_STORAGE_ACCOUNT_NAME"),
            ("foundryAccountName", "FOUNDRY_ACCOUNT_NAME"),
        ):
            value = values.get(key, "")
            if key == "AZURE_STORAGE_ACCOUNT_NAME" and not value:
                match = re.fullmatch(r"https://([a-z0-9]{3,24})\.blob\.core\.windows\.net/?", values.get("AZURE_STORAGE_ACCOUNT_URL", ""))
                value = match[1] if match else ""
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,62}", value):
                raise RegistryError("invalid_configuration", "Existing service name is required: " + key + ".")
            result[parameter] = value
        search = values.get("AZURE_SEARCH_SERVICE_NAME", "")
        if flag(values, "SEARCH_ENABLED") and not search:
            raise RegistryError("missing_configuration", "Enabled Search requires AZURE_SEARCH_SERVICE_NAME.")
        if search and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,59}", search):
            raise RegistryError("invalid_configuration", "Invalid existing Search service name.")
        result["searchServiceName"] = search
    return result


def preflight(values: dict[str, str], target: dict[str, str], phase: str, blueprint: str, agent: str) -> dict:
    with GraphClient(tenant_id=target["tenant"]) as graph:
        report = identity_preflight(
            graph, managed_identity_principal_id=target["principalId"],
            blueprint_app_id=blueprint or None, agent_identity_id=agent or None,
            stage="bootstrap" if phase == "blueprint" else "deploy",
            registry=False,
        )
    if report.get("status") != "satisfied" or (phase == "identity" and report.get("deploymentReady") is not True):
        raise GateBlocked(report)
    return report


def verified_outputs(deployment: dict, values: dict[str, str], target: dict[str, str], phase: str) -> dict[str, str]:
    properties = deployment.get("properties", {})
    if properties.get("provisioningState") != "Succeeded":
        raise RegistryError("deployment_incomplete", "Identity deployment is not Succeeded; no outputs were exported.")
    raw = properties.get("outputs", {})
    outputs = {}
    required = set(IDENTITY_OUTPUTS) if phase == "identity" else set(IDENTITY_OUTPUTS) - {
        "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_PRINCIPAL_ID", "AGENT_IDENTITY_DISPLAY_NAME",
    }
    for name in required:
        item = raw.get(name, {})
        value = item.get("value")
        if str(item.get("type", "")).lower() != "string" or not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise RegistryError("invalid_outputs", "Missing or invalid identity deployment output: " + name + ".")
        outputs[name] = value
    for name in required - {"AGENT_IDENTITY_DISPLAY_NAME", "AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID"}:
        outputs[name] = nonzero_guid(outputs[name], name)
    for name, actual in (
        ("AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID", target["resourceId"]),
        ("AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID", target["principalId"]),
        ("MCP_SERVER_IDENTITY_CLIENT_ID", target["clientId"]),
    ):
        if outputs[name].lower() != actual.lower():
            raise RegistryError("identity_mismatch", "Identity deployment used a different bootstrap UAMI.")
    for name in ("AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_APP_ID"):
        if values.get(name) and name in outputs and values[name].lower() != outputs[name]:
            raise RegistryError("identity_mismatch", "Deployment would replace an existing identity output: " + name + ".")
    if phase == "identity" and outputs["AGENT_IDENTITY_PRINCIPAL_ID"] != outputs["AGENT_IDENTITY_APP_ID"]:
        raise RegistryError("identity_mismatch", "Agent object and app IDs differ.")
    checked = {**values, **outputs}
    report = preflight(checked, target, phase, outputs["AGENT_IDENTITY_BLUEPRINT_APP_ID"], outputs.get("AGENT_IDENTITY_APP_ID", ""))
    verify_report(report, checked, target, require_agent=phase == "identity")
    return outputs


def deploy(values: dict[str, str], args) -> dict:
    if values.get("AGENT_IDENTITY_PROVISIONING_MODE", "adopt") != "managed":
        raise RegistryError("managed_provisioning_disabled", "Use Agent 365 onboarding for the default adopt path. AGENT_IDENTITY_PROVISIONING_MODE=managed explicitly opts into this advanced directory-provisioning workflow.")
    if not flag(values, "AGENT_IDENTITY_ENABLED"):
        raise RegistryError("identity_disabled", "AGENT_IDENTITY_ENABLED must explicitly be true for stage one.")
    blueprint = adoption_id(values, "EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID", args.blueprint_app_id)
    agent = adoption_id(values, "EXISTING_AGENT_IDENTITY_ID", "AGENT_IDENTITY_APP_ID", args.agent_identity_id)
    if args.phase == "blueprint" and agent and not args.import_outputs:
        raise RegistryError("invalid_phase", "An existing child identity requires the identity phase; do not bootstrap over a completed deployment.")
    if args.phase == "identity" and not blueprint and not args.import_outputs:
        raise RegistryError("missing_configuration", "The identity phase requires a precreated and consented blueprint; complete blueprint bootstrap first.")
    target = resolve_target(values)
    name = "agent-id-" + args.phase + "-" + args.environment
    if len(name) > 64:
        raise RegistryError("invalid_configuration", "Environment name is too long for the scoped deployment name.")
    base = ["az", "deployment", "group"]
    common = ["--name", name, "--subscription", target["subscription"], "--resource-group", target["group"], "--output", "json", "--only-show-errors"]
    if args.import_outputs:
        # Recovery must work after even the first azd export failed. Verify the
        # succeeded deployment's IDs below instead of requiring them up front.
        deployment = json_object(run_cli([*base, "show", *common]))
    else:
        report = preflight(values, target, args.phase, blueprint, agent)
        config = parameters(values, target, args.phase, blueprint, agent)
        if not args.apply:
            return {"status": "satisfied", "readOnly": True, "phase": args.phase,
                    "deploymentName": name, "deploymentReady": report["deploymentReady"],
                    "message": "Prerequisites checked only; no identity was deployed or exported."}
        # Parameters contain only non-secret names/IDs. A temporary parameter
        # file avoids Windows az.cmd quoting JSON or arbitrary display names.
        with tempfile.TemporaryDirectory(prefix="agent-id-parameters-") as directory:
            path = Path(directory) / "parameters.json"
            path.write_text(json.dumps({"parameters": {key: {"value": value} for key, value in config.items()}}), encoding="utf-8")
            deployment = json_object(run_cli([
                *base, "create", *common, "--mode", "Incremental",
                "--template-file", str(ROOT / "infra/agent-identity.bicep"),
                "--parameters", "@" + str(path),
            ], timeout=1800))
    expected = dict(values)
    if blueprint:
        expected["AGENT_IDENTITY_BLUEPRINT_APP_ID"] = blueprint
    if agent:
        expected["AGENT_IDENTITY_APP_ID"] = agent
    outputs = verified_outputs(deployment, expected, target, args.phase)
    handoff = dict(outputs, EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID=outputs["AGENT_IDENTITY_BLUEPRINT_APP_ID"])
    if args.phase == "identity":
        handoff["EXISTING_AGENT_IDENTITY_ID"] = outputs["AGENT_IDENTITY_APP_ID"]
    for key, value in sorted(handoff.items()):
        run_cli(["azd", "env", "set", key, value, "--environment", args.environment])
    return {"status": "verified", "phase": args.phase, "deploymentName": name,
            "outputs": outputs, "runtimeDeployed": False,
            "nextStep": "Administrator consent on the blueprint principal, then identity phase." if args.phase == "blueprint" else "Run the stage-two deployment gate before build/rollout and registry publication."}


def main(argv: list[str] | None = None) -> int:
    try:
        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--environment", required=True)
        parser.add_argument("--phase", choices=("blueprint", "identity"), required=True)
        parser.add_argument("--blueprint-app-id")
        parser.add_argument("--agent-identity-id")
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--apply", action="store_true")
        action.add_argument("--import-outputs", action="store_true")
        args = parser.parse_args(argv)
        values = load_configuration(from_azd=True, environment=args.environment)
        print(json.dumps(deploy(values, args), sort_keys=True))
        return 0
    except GateBlocked as error:
        print(json.dumps(error.report, sort_keys=True))
        return 2
    except RegistryError as error:
        print(json.dumps({"status": "blocked", "error": error.as_dict()}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "inconclusive", "error": "Identity deployment failed; no automatic retry. Inspect the scoped deployment and use --import-outputs after recovery."}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())