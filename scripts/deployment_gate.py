"""Strict stage-two gate. No sign-in, grants, builds, or rollout in --check.

Use --from-azd (optionally --environment) for authoritative named azd settings.
Otherwise use the process environment, as exported by the postprovision hooks.
--export-env emits only allowlisted, single-line KEY=value records; the hooks
import them as data, never shell code. Absent keys clear stale shell values.
--publish-registry is the only write action here and reruns the same gate first.
Existing identity relationships and blueprint trust are checked. Lifecycle
Graph permissions are required ONLY for explicit managed provisioning;
this is NOT proof of downstream token issuance, Azure RBAC, or service health.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.agent_identity_preflight import adopted_identity_preflight, identity_preflight  # noqa: E402
from scripts.publish_agent_registry import publication_inputs  # noqa: E402
from scripts.configure_approval_runtime import REQUIRED_KEYS as APPROVAL_KEYS  # noqa: E402
from src.agent_registry import (  # noqa: E402
    API_CHOICES, AgentRegistryPublisher, GraphClient, PublicationJournal,
    RegistryError, registry_id,
)
from src.agent_observability import TelemetryConfig  # noqa: E402

CONFIG_KEYS = tuple(sorted(set(APPROVAL_KEYS) | {
    "AZURE_ENV_NAME", "AZURE_LOCATION", "AZURE_STORAGE_ACCOUNT_URL",
    "AZURE_STORAGE_ACCOUNT_NAME", "MCP_SERVER_IDENTITY_CLIENT_ID",
    "MCP_BASE_URL", "MCP_INTERNAL_LB_IP", "MCP_LB_SUBNET_NAME",
    "MCP_AGENT_RUNTIME", "CONTAINER_REGISTRY", "IMAGE_NAME", "IMAGE_TAG",
    "COMMIT_SHA", "ACR_BUILD_MODE", "ACR_TASK_AGENT_POOL", "ACR_NAME",
    "FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_ACCOUNT_NAME",
    "FOUNDRY_MODEL_DEPLOYMENT_NAME", "EMBEDDING_MODEL_DEPLOYMENT_NAME",
    "COSMOSDB_ENDPOINT", "COSMOSDB_ACCOUNT_NAME", "COSMOSDB_DATABASE_NAME",
    "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_SERVICE_NAME", "AZURE_SEARCH_INDEX_NAME",
    "AZURE_SEARCH_KNOWLEDGE_BASE_NAME", "SEARCH_ENABLED",
    "FABRIC_ENABLED", "FABRIC_CAPACITY_NAME", "FABRIC_WORKSPACE_ID",
    "ONTOLOGY_CONTAINER_NAME", "APPROVAL_LOGIC_APP_ENABLED", "APPROVAL_RUNTIME_CONFIG",
    "AGENT_IDENTITY_ENABLED", "AGENT_IDENTITY_PROVISIONING_MODE", "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_PRINCIPAL_ID",
    "AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID",
    "AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID", "AGENT_IDENTITY_DISPLAY_NAME",
    "AGENT_BLUEPRINT_DISPLAY_NAME", "AGENT_BLUEPRINT_UNIQUE_NAME",
    "AGENT_SPONSOR_PRINCIPAL_ID", "EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID",
    "EXISTING_AGENT_IDENTITY_ID", "AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID",
    "AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID", "AGENT_IDENTITY_CONFIGURATION_NAME",
    "AGENT_REGISTRY_ENABLED", "AGENT_REGISTRY_API", "AGENT_ENDPOINT_URL",
    "AGENT_REGISTRY_OWNER_IDS", "AGENT_REGISTRY_ID", "AGENT_REGISTRY_STATE_FILE",
    "AGENT_REGISTRY_SOURCE_AGENT_ID", "AGENT_REGISTRY_ORIGINATING_STORE",
    "AGENT_REGISTRY_MANAGED_BY_APP_ID", "AGENT_REGISTRY_CREATED_BY_ID",
    "AGENT_REGISTRY_SOURCE_CREATED_AT", "AGENT_REGISTRY_SOURCE_MODIFIED_AT",
    "AGENT_OBSERVABILITY_MODE",
}))
DEFAULTS = {
    "AGENT_IDENTITY_ENABLED": "false", "AGENT_REGISTRY_ENABLED": "false",
    "APPROVAL_LOGIC_APP_ENABLED": "false", "AGENT_REGISTRY_API": "agent365",
    "MCP_AGENT_RUNTIME": "python", "ACR_BUILD_MODE": "acr-task",
    "K8S_NAMESPACE": "mcp-agents", "SEARCH_ENABLED": "false",
    "AGENT_IDENTITY_PROVISIONING_MODE": "adopt",
    "AGENT_OBSERVABILITY_MODE": "off",
}


class GateBlocked(RegistryError):
    def __init__(self, report: dict):
        super().__init__("preflight_blocked", "Deployment preflight did not pass; no write was attempted.")
        self.report = report


def run_cli(command: list[str], *, timeout: int = 120) -> str:
    """Bounded, noninteractive subprocess; raw output/errors never escape on failure."""
    executable = shutil.which(command[0])
    if executable is None:
        raise RegistryError("cli_unavailable", "Required deployment CLI is unavailable.")
    child_env = dict(os.environ, AZURE_LOGGING_ENABLE_LOG_FILE="false", AZURE_CORE_COLLECT_TELEMETRY="false")
    for name in ("LOGIC_APP_APPROVAL_WEBHOOK", "APPROVAL_LOGIC_APP_TRIGGER_URL", "MCP_BEARER_TOKEN", "APIM_SUBSCRIPTION_KEY"):
        child_env.pop(name, None)
    try:
        result = subprocess.run(
            [executable, *command[1:]], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", env=child_env,
            timeout=timeout, check=False,
        )
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise ValueError
        return result.stdout
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise RegistryError("command_failed", "Deployment command failed; output suppressed. Check existing CLI access and reconcile any requested write before retrying.") from None


def json_object(text: str) -> dict:
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise RegistryError("invalid_response", "Deployment response was not a JSON object; content suppressed.") from None


def nonempty(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RegistryError("missing_configuration", "Required deployment setting is empty: " + name + ".")
    return value


def nonzero_guid(value: str, label: str) -> str:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
            raise ValueError
        parsed = UUID(value)
        if not parsed.int:
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError):
        raise RegistryError("invalid_configuration", label + " must be a nonzero GUID.") from None


def flag(values: dict[str, str], name: str) -> bool:
    value = values.get(name, DEFAULTS.get(name, "false")).strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise RegistryError("invalid_configuration", name + " must be true/false or 1/0.")
    return value in ("true", "1")


def load_configuration(*, from_azd: bool = False, environment: str | None = None) -> dict[str, str]:
    if environment is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", environment):
        raise RegistryError("invalid_configuration", "Invalid azd environment name.")
    if from_azd or environment is not None:
        command = ["azd", "env", "get-values", "--output", "json"]
        if environment:
            command += ["--environment", environment]
        source = json_object(run_cli(command))
        if environment and source.get("AZURE_ENV_NAME") != environment:
            raise RegistryError("environment_mismatch", "azd returned a different environment.")
    else:
        source = dict(os.environ)
    values = {}
    for name in CONFIG_KEYS:
        value = source.get(name, DEFAULTS.get(name, ""))
        if name == "APPROVAL_RUNTIME_CONFIG" and value:
            grouped = json_object(value) if isinstance(value, str) else value
            if not isinstance(grouped, dict):
                raise RegistryError("invalid_configuration", "Invalid APPROVAL_RUNTIME_CONFIG.")
            value = json.dumps({key: grouped[key] for key in APPROVAL_KEYS if key in grouped}, separators=(",", ":"))
        if isinstance(value, bool):
            value = str(value).lower()
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) or len(value) > 32768 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise RegistryError("invalid_configuration", "Invalid single-line deployment setting: " + name + ".")
        values[name] = value
    # Validate explicit empty/typo flags BEFORE a shell can unset empty values.
    for name in ("AGENT_IDENTITY_ENABLED", "AGENT_REGISTRY_ENABLED", "APPROVAL_LOGIC_APP_ENABLED"):
        values[name] = str(flag(values, name)).lower()
    try:
        values["AGENT_OBSERVABILITY_MODE"] = TelemetryConfig.from_environment(values).mode
    except ValueError as error:
        raise RegistryError("invalid_configuration", str(error)) from None
    if values["AGENT_IDENTITY_PROVISIONING_MODE"] not in ("adopt", "managed"):
        raise RegistryError("invalid_configuration", "AGENT_IDENTITY_PROVISIONING_MODE must be adopt or managed.")
    if values["AGENT_REGISTRY_API"] not in API_CHOICES:
        raise RegistryError("invalid_configuration", "Unsupported Agent Registry API.")
    return values


def resolve_target(values: dict[str, str]) -> dict[str, str]:
    """Resolve the actual UAMI, pinned to the configured subscription and tenant."""
    subscription = nonzero_guid(nonempty(values, "AZURE_SUBSCRIPTION_ID"), "Subscription ID")
    tenant = nonzero_guid(nonempty(values, "AZURE_TENANT_ID"), "Tenant ID")
    group = nonempty(values, "AZURE_RESOURCE_GROUP_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_().-]{1,90}", group) or group.endswith("."):
        raise RegistryError("invalid_configuration", "Invalid resource group name.")
    client = nonzero_guid(nonempty(values, "MCP_SERVER_IDENTITY_CLIENT_ID"), "Bootstrap UAMI client ID")
    account = json_object(run_cli(["az", "account", "show", "--subscription", subscription, "--output", "json"]))
    if account.get("id", "").lower() != subscription or account.get("tenantId", "").lower() != tenant or account.get("state") != "Enabled":
        raise RegistryError("tenant_mismatch", "Subscription is unavailable or belongs to a different tenant.")
    try:
        identities = json.loads(run_cli([
            "az", "identity", "list", "--subscription", subscription,
            "--resource-group", group, "--output", "json", "--only-show-errors",
        ]))
        if not isinstance(identities, list) or any(not isinstance(item, dict) for item in identities):
            raise ValueError
        matches = [item for item in identities if str(item.get("clientId", "")).lower() == client]
        if len(matches) != 1:
            raise ValueError
        selected = matches[0]
        name = selected["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", name):
            raise ValueError
        expected = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
        if selected.get("id", "").lower() != expected.lower():
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise RegistryError("identity_mismatch", "Cannot uniquely resolve the configured bootstrap UAMI in the target resource group.") from None
    principal = nonzero_guid(selected["principalId"], "Bootstrap UAMI principal ID")
    for key, actual in (
        ("AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID", expected),
        ("AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID", principal),
        ("AGENT_IDENTITY_CONFIGURATION_NAME", name),
    ):
        if values.get(key) and values[key].lower() != actual.lower():
            raise RegistryError("identity_mismatch", "Bootstrap UAMI does not match " + key + ".")
    return {"subscription": subscription, "tenant": tenant, "group": group,
            "clientId": client, "principalId": principal, "resourceId": expected, "name": name}


def identity_ids(values: dict[str, str]) -> tuple[str, str, str]:
    names = ("AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID", "AGENT_IDENTITY_APP_ID")
    blueprint, object_id, agent = (nonzero_guid(nonempty(values, name), name) for name in names)
    if len({blueprint, object_id, agent}) != 3:
        raise RegistryError("identity_mismatch", "Agent and blueprint application IDs must identify distinct objects.")
    if values.get("AGENT_IDENTITY_PRINCIPAL_ID") and nonzero_guid(values["AGENT_IDENTITY_PRINCIPAL_ID"], "Agent principal ID") != agent:
        raise RegistryError("identity_mismatch", "Agent app and principal IDs must match.")
    for key, actual in (("EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID", blueprint), ("EXISTING_AGENT_IDENTITY_ID", agent)):
        if values.get(key) and nonzero_guid(values[key], key) != actual:
            raise RegistryError("identity_mismatch", "Adoption input differs from the generated identity output: " + key + ".")
    return blueprint, object_id, agent


def registry_inputs(values: dict[str, str]) -> tuple[dict, dict, Path | None, str | None]:
    """Use the publisher's exact manifest validation and journal semantics."""
    api = values.get("AGENT_REGISTRY_API") or "agent365"
    if api not in API_CHOICES:
        raise RegistryError("invalid_configuration", "Unsupported Agent Registry API.")
    owners = nonempty(values, "AGENT_REGISTRY_OWNER_IDS").split(",")
    owners = [nonzero_guid(owner.strip(), "Registry owner ID") for owner in owners]
    fields = {
        "endpoint": values.get("AGENT_ENDPOINT_URL") or nonempty(values, "MCP_BASE_URL"),
        "agent_identity_id": nonempty(values, "AGENT_IDENTITY_APP_ID"),
        "display_name": nonempty(values, "AGENT_IDENTITY_DISPLAY_NAME"),
        "source_agent_id": values.get("AGENT_REGISTRY_SOURCE_AGENT_ID") or None,
        "originating_store": values.get("AGENT_REGISTRY_ORIGINATING_STORE") or "Azure Agents Control Plane",
        "owner_id": owners, "api": api,
        "blueprint_object_id": nonempty(values, "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID"),
        "managed_by_app_id": values.get("AGENT_REGISTRY_MANAGED_BY_APP_ID") or None,
        "registry_id": values.get("AGENT_REGISTRY_ID") or None,
        "state_file": values.get("AGENT_REGISTRY_STATE_FILE") or None,
        "created_by_id": values.get("AGENT_REGISTRY_CREATED_BY_ID") or None,
        "source_created_at": values.get("AGENT_REGISTRY_SOURCE_CREATED_AT") or None,
        "source_modified_at": values.get("AGENT_REGISTRY_SOURCE_MODIFIED_AT") or None,
        "instance_manifest": str(ROOT / "agent-approvals/manifests/agent_instance.json"),
        "card_manifest": str(ROOT / "agent-approvals/manifests/agent_card_manifest.json"),
    }
    instance, card, state = publication_inputs(SimpleNamespace(**fields), environ=values)
    saved = registry_id(fields["registry_id"]) if fields["registry_id"] else None
    if state is not None:
        journal = PublicationJournal(state, api=api, tenant_id=values["AZURE_TENANT_ID"], instance=instance)
        if journal.saved_id:
            if saved and saved != journal.saved_id:
                raise RegistryError("journal_mismatch", "Saved registry ID differs from the deployment journal.")
            saved = journal.saved_id
        if journal.data and not saved and api == "agent365":
            raise RegistryError("creation_outcome_unknown", "A registry creation is pending. Recover its ID before building or rolling out; never delete the journal to retry.")
    return instance, card, state, saved


def verify_report(report: dict, values: dict[str, str], target: dict[str, str], *, require_agent: bool) -> None:
    if report.get("status") != "satisfied" or (require_agent and report.get("deploymentReady") is not True):
        raise GateBlocked(report)
    checks = {check["component"]: check for check in report.get("checks", [])}
    mi = checks.get("configurationManagedIdentity", {})
    bp = checks.get("blueprintPrincipal", {})
    if mi.get("principalId") != target["principalId"] or mi.get("federation") != "matchesConfiguration":
        raise RegistryError("federation_unverified", "Blueprint-to-UAMI federation is missing or mismatched; the blueprint owner must configure trust before build or rollout.")
    if bp.get("blueprintObjectId") != values["AGENT_IDENTITY_BLUEPRINT_OBJECT_ID"] or bp.get("blueprintAppId") != values["AGENT_IDENTITY_BLUEPRINT_APP_ID"]:
        raise RegistryError("identity_mismatch", "Blueprint application object/client IDs do not match directory readback.")
    if values.get("AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID") and bp.get("principalId") != nonzero_guid(values["AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID"], "Blueprint principal ID"):
        raise RegistryError("identity_mismatch", "Blueprint principal ID does not match directory readback.")
    if require_agent:
        child = checks.get("precreatedAgent", {})
        if child.get("status") != "satisfied" or child.get("agentIdentityId") != values["AGENT_IDENTITY_APP_ID"]:
            raise RegistryError("identity_unverified", "Child Agent ID was not verified in the directory.")


def check_deployment(values: dict[str, str]) -> dict:
    enabled, registry = flag(values, "AGENT_IDENTITY_ENABLED"), flag(values, "AGENT_REGISTRY_ENABLED")
    try:
        telemetry = TelemetryConfig.from_environment(values)
    except ValueError as error:
        raise RegistryError("invalid_configuration", str(error)) from None
    telemetry_identity = telemetry.mode == "agent365"
    identity_mode = values.get("AGENT_IDENTITY_PROVISIONING_MODE", "adopt")
    if identity_mode not in ("adopt", "managed"):
        raise RegistryError("invalid_configuration", "AGENT_IDENTITY_PROVISIONING_MODE must be adopt or managed.")
    if values.get("MCP_AGENT_RUNTIME", "python").lower() != "python" and (enabled or telemetry.mode != "off" or flag(values, "APPROVAL_LOGIC_APP_ENABLED")):
        raise RegistryError("unsupported_runtime", "Agent ID, observability and approval enforcement require the Python runtime.")
    if not enabled and not registry and not telemetry_identity:
        return {"status": "skipped", "reason": "Agent ID and registry are disabled", "readOnly": True}
    try:
        blueprint, object_id, agent = identity_ids(values)
    except RegistryError as error:
        if identity_mode == "adopt" and error.code == "missing_configuration":
            raise RegistryError("missing_configuration", str(error) + " Complete authorized Agent 365 onboarding and adopt its real IDs; UAMI blueprint-management grants are not required in adopt mode.") from None
        raise
    values = dict(values, AGENT_IDENTITY_BLUEPRINT_APP_ID=blueprint, AGENT_IDENTITY_BLUEPRINT_OBJECT_ID=object_id, AGENT_IDENTITY_APP_ID=agent)
    target = resolve_target(values)
    if agent in (target["clientId"], target["principalId"]) or blueprint in (target["clientId"], target["principalId"]):
        raise RegistryError("identity_mismatch", "Bootstrap UAMI is not a substitute for an Agent ID or blueprint.")
    publication = registry_inputs(values) if registry else None
    with GraphClient(tenant_id=target["tenant"]) as graph:
        if identity_mode == "adopt" or not enabled:
            report = adopted_identity_preflight(
                graph, managed_identity_principal_id=target["principalId"],
                blueprint_app_id=blueprint, agent_identity_id=agent,
            )
            if registry:
                publisher_check = AgentRegistryPublisher(graph, api=values.get("AGENT_REGISTRY_API") or "agent365").preflight(
                    saved_id=publication[3], managed_by=values.get("AGENT_REGISTRY_MANAGED_BY_APP_ID") or None,
                )
                report["checks"].append(publisher_check)
                if publisher_check.get("status") != "satisfied":
                    report.update(status="blocked", deploymentReady=False)
        else:
            report = identity_preflight(
                graph, stage="deploy", managed_identity_principal_id=target["principalId"],
                blueprint_app_id=blueprint, agent_identity_id=agent, registry=registry,
                registry_api=values.get("AGENT_REGISTRY_API") or "agent365",
                saved_registry_id=publication[3] if publication else None,
                managed_by_app_id=values.get("AGENT_REGISTRY_MANAGED_BY_APP_ID") or None,
            )
        verify_report(report, values, target, require_agent=True)
        if registry:
            publisher_check = next((item for item in report.get("checks", []) if item.get("component") == "registryPublisher"), {})
            if publisher_check.get("status") != "satisfied":
                raise RegistryError("registry_unverified", "Registry consent was not verified; build/rollout is blocked.")
            caller = graph.caller()
            if caller is None or agent in (caller["clientId"], caller["principalId"]):
                raise RegistryError("runtime_identity_not_allowed", "Registry publication requires a separate deployment caller.")
            if publication[3]:
                AgentRegistryPublisher(graph, api=values["AGENT_REGISTRY_API"]).verify_existing_binding(publication[3], publication[0], publication[1])
    return report


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise RegistryError("invalid_arguments", "Invalid deployment arguments; use --help. Values suppressed.")


def main(argv: list[str] | None = None) -> int:
    try:
        if sys.version_info < (3, 10):
            raise RegistryError("unsupported_python", "Deployment helpers require Python 3.10+.")
        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--from-azd", action="store_true")
        parser.add_argument("--environment")
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument("--check", action="store_true")
        actions.add_argument("--export-env", action="store_true")
        actions.add_argument("--publish-registry", action="store_true")
        args = parser.parse_args(argv)
        values = load_configuration(from_azd=args.from_azd, environment=args.environment)
        if args.export_env:
            print("\n".join(name + "=" + values[name] for name in CONFIG_KEYS))
            return 0
        report = check_deployment(values)
        if args.publish_registry:
            if not flag(values, "AGENT_REGISTRY_ENABLED"):
                raise RegistryError("registry_disabled", "Registry publication is not enabled.")
            instance, card, state, saved = registry_inputs(values)
            with GraphClient(tenant_id=values["AZURE_TENANT_ID"]) as graph:
                report = AgentRegistryPublisher(graph, api=values["AGENT_REGISTRY_API"]).publish(instance, card, saved_id=saved, journal_path=state)
        print(json.dumps(report, sort_keys=True))
        return 0
    except GateBlocked as error:
        print(json.dumps(error.report, sort_keys=True))
        return 2
    except RegistryError as error:
        print(json.dumps({"status": "blocked", "error": error.as_dict()}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "inconclusive", "error": "Deployment check failed; details suppressed."}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())