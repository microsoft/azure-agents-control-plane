"""Offline only: strict fake CLI/Graph boundaries, no live auth or deployment."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import deployment_gate as gate
from scripts import deploy_agent_identity as stage
from scripts import build_and_push as build

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
MI_CLIENT = "33333333-3333-3333-3333-333333333333"
MI = "44444444-4444-4444-4444-444444444444"
BLUEPRINT = "55555555-5555-5555-5555-555555555555"
OBJECT = "66666666-6666-6666-6666-666666666666"
PRINCIPAL = "77777777-7777-7777-7777-777777777777"
AGENT = "88888888-8888-8888-8888-888888888888"
OWNER = "99999999-9999-9999-9999-999999999999"
ROOT = Path(__file__).resolve().parents[1]
RESOURCE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-unit/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mcp-unit"
DIGEST = "sha256:" + "a" * 64


def values():
    return {
        "AZURE_ENV_NAME": "unit", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_TENANT_ID": TENANT, "AZURE_RESOURCE_GROUP_NAME": "rg-unit",
        "MCP_SERVER_IDENTITY_CLIENT_ID": MI_CLIENT,
        "AGENT_IDENTITY_ENABLED": "true", "AGENT_REGISTRY_ENABLED": "true",
        "AGENT_IDENTITY_PROVISIONING_MODE": "managed",
        "AGENT_REGISTRY_API": "agent365", "MCP_AGENT_RUNTIME": "python",
        "AGENT_IDENTITY_BLUEPRINT_APP_ID": BLUEPRINT,
        "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID": OBJECT,
        "AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID": PRINCIPAL,
        "AGENT_IDENTITY_APP_ID": AGENT, "AGENT_IDENTITY_PRINCIPAL_ID": AGENT,
        "AGENT_IDENTITY_DISPLAY_NAME": "Next Best Action Unit",
        "AGENT_SPONSOR_PRINCIPAL_ID": OWNER, "AGENT_REGISTRY_OWNER_IDS": OWNER,
        "MCP_BASE_URL": "https://unit.azure-api.net/mcp",
        "COSMOSDB_ACCOUNT_NAME": "cosmos-unit", "FOUNDRY_ACCOUNT_NAME": "foundry-unit",
        "AZURE_STORAGE_ACCOUNT_URL": "https://storageunit.blob.core.windows.net/",
        "CONTAINER_REGISTRY": "registryunit.azurecr.io", "IMAGE_TAG": "unit-1234",
        "ACR_BUILD_MODE": "acr-task", "ACR_TASK_AGENT_POOL": "private-pool",
    }


def target():
    return {"subscription": SUBSCRIPTION, "tenant": TENANT, "group": "rg-unit",
            "name": "id-mcp-unit", "principalId": MI, "clientId": MI_CLIENT,
            "resourceId": RESOURCE}


def report():
    return {"status": "satisfied", "deploymentReady": True, "readOnly": True,
            "checks": [
                {"component": "configurationManagedIdentity", "status": "satisfied", "principalId": MI, "federation": "matchesConfiguration"},
                {"component": "blueprintPrincipal", "status": "satisfied", "blueprintAppId": BLUEPRINT, "blueprintObjectId": OBJECT, "principalId": PRINCIPAL},
                {"component": "precreatedAgent", "status": "satisfied", "agentIdentityId": AGENT},
                {"component": "registryPublisher", "status": "satisfied"},
            ]}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    for module in (gate, stage, build):
        monkeypatch.setattr(module, "run_cli", Mock(side_effect=AssertionError("Unexpected CLI")))
    for module in (gate, stage):
        monkeypatch.setattr(module, "GraphClient", Mock(side_effect=AssertionError("Unexpected Graph")))


def graph_context(monkeypatch, module):
    graph = Mock()
    graph.caller.return_value = {"clientId": MI_CLIENT, "principalId": OWNER}
    context = Mock()
    context.__enter__ = Mock(return_value=graph)
    context.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(module, "GraphClient", Mock(return_value=context))
    return graph


def configured_gate(monkeypatch, tmp_path):
    config = values()
    config["AGENT_REGISTRY_STATE_FILE"] = str(tmp_path / "registry.json")
    monkeypatch.setattr(gate, "resolve_target", Mock(return_value=target()))
    graph_context(monkeypatch, gate)
    monkeypatch.setattr(gate, "identity_preflight", Mock(return_value=report()))
    return config


@pytest.mark.parametrize("name", ["AGENT_IDENTITY_BLUEPRINT_APP_ID", "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID", "AGENT_IDENTITY_APP_ID"])
def test_gate_missing_ids_fails_before_cli_or_graph(name):
    config = values()
    config[name] = ""
    with pytest.raises(gate.RegistryError, match=name):
        gate.check_deployment(config)
    gate.run_cli.assert_not_called()
    gate.GraphClient.assert_not_called()


@pytest.mark.parametrize("name", ["AGENT_IDENTITY_ENABLED", "AGENT_REGISTRY_ENABLED"])
def test_invalid_flag_never_turns_off_guard(name):
    config = values()
    config[name] = "enabled"
    with pytest.raises(gate.RegistryError, match=name):
        gate.check_deployment(config)
    gate.run_cli.assert_not_called()


def test_disabled_flags_skip_without_external_calls():
    assert gate.check_deployment({})["status"] == "skipped"
    gate.run_cli.assert_not_called()
    gate.GraphClient.assert_not_called()


@pytest.mark.parametrize("data_identity", [True, False], ids=["agent-resource-access", "uami-resource-access"])
def test_default_adoption_skips_management_grants_and_duplicate_publication(monkeypatch, data_identity):
    config = values()
    config.pop("AGENT_IDENTITY_PROVISIONING_MODE")
    config.update(AGENT_IDENTITY_ENABLED=str(data_identity).lower(), AGENT_REGISTRY_ENABLED="false", AGENT_OBSERVABILITY_MODE="agent365")
    monkeypatch.setattr(gate, "resolve_target", Mock(return_value=target()))
    graph_context(monkeypatch, gate)
    adoption = Mock(return_value=report())
    management = Mock(side_effect=AssertionError("No lifecycle grants in adopt path"))
    publisher = Mock(side_effect=AssertionError("Registration already belongs to Agent 365 onboarding"))
    monkeypatch.setattr(gate, "adopted_identity_preflight", adoption)
    monkeypatch.setattr(gate, "identity_preflight", management)
    monkeypatch.setattr(gate, "AgentRegistryPublisher", publisher)
    assert gate.check_deployment(config)["deploymentReady"] is True
    adoption.assert_called_once()
    management.assert_not_called()
    publisher.assert_not_called()


def test_managed_directory_deployment_requires_explicit_opt_in():
    config = values()
    config.pop("AGENT_IDENTITY_PROVISIONING_MODE")
    with pytest.raises(gate.RegistryError, match="advanced"):
        stage.deploy(config, args(apply=True))
    stage.run_cli.assert_not_called()
    stage.GraphClient.assert_not_called()


def test_console_tracing_needs_no_agent_ids_or_directory_reads():
    assert gate.check_deployment({"AGENT_OBSERVABILITY_MODE": "console"})["status"] == "skipped"
    gate.GraphClient.assert_not_called()


def test_bicep_adoption_never_invokes_directory_reconciliation():
    source = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
    assert "param agentIdentityProvisioningMode string = 'adopt'" in source
    assert "var manageAgentIdentity = agentIdentityEnabled && agentIdentityProvisioningMode == 'managed'" in source
    for symbol in ("agentIdentityBlueprint", "nextBestActionAgentIdentity", "agentFederatedCredential"):
        line = next(line for line in source.splitlines() if line.startswith("module " + symbol + " "))
        assert "if (manageAgentIdentity)" in line


def test_registry_cannot_be_enabled_without_real_identity():
    with pytest.raises(gate.RegistryError, match="AGENT_IDENTITY_BLUEPRINT_APP_ID"):
        gate.check_deployment({"AGENT_REGISTRY_ENABLED": "true"})


def test_gate_checks_adopted_child_and_exact_blueprint_object(monkeypatch, tmp_path):
    config = configured_gate(monkeypatch, tmp_path)
    assert gate.check_deployment(config)["deploymentReady"] is True
    call = gate.identity_preflight.call_args.kwargs
    assert call["stage"] == "deploy"
    assert call["agent_identity_id"] == AGENT
    assert call["blueprint_app_id"] == BLUEPRINT
    assert call["managed_identity_principal_id"] == MI
    assert call["registry"] is True
    assert not Path(config["AGENT_REGISTRY_STATE_FILE"]).exists()


@pytest.mark.parametrize("status,ready", [("blocked", False), ("inconclusive", False), ("satisfied", False)])
def test_bootstrap_success_or_inconclusive_is_never_rollout_ready(monkeypatch, tmp_path, status, ready):
    config = configured_gate(monkeypatch, tmp_path)
    gate.identity_preflight.return_value.update(status=status, deploymentReady=ready)
    with pytest.raises(gate.GateBlocked):
        gate.check_deployment(config)


@pytest.mark.parametrize("component,key,value", [
    ("configurationManagedIdentity", "federation", "deploymentWillReconcile"),
    ("configurationManagedIdentity", "principalId", AGENT),
    ("blueprintPrincipal", "blueprintObjectId", PRINCIPAL),
    ("blueprintPrincipal", "principalId", OBJECT),
    ("precreatedAgent", "agentIdentityId", MI),
])
def test_mismatched_identity_or_unreconciled_trust_blocks(monkeypatch, tmp_path, component, key, value):
    config = configured_gate(monkeypatch, tmp_path)
    check = next(item for item in gate.identity_preflight.return_value["checks"] if item["component"] == component)
    check[key] = value
    with pytest.raises(gate.RegistryError):
        gate.check_deployment(config)


def test_pending_registry_journal_blocks_before_graph(monkeypatch, tmp_path):
    config = configured_gate(monkeypatch, tmp_path)
    instance, _, path, _ = gate.registry_inputs(config)
    journal = gate.PublicationJournal(path, api="agent365", tenant_id=TENANT, instance=instance)
    journal.reserve()
    with pytest.raises(gate.RegistryError, match="pending"):
        gate.check_deployment(config)
    gate.GraphClient.assert_not_called()
    assert json.loads(path.read_text())["phase"] == "pending"


def test_registry_input_parsing_is_independent_of_stale_process_environment(monkeypatch, tmp_path):
    config = values()
    config["AGENT_REGISTRY_STATE_FILE"] = str(tmp_path / "registry.json")
    config["AGENT_REGISTRY_MANAGED_BY_APP_ID"] = OWNER
    monkeypatch.setenv("AZURE_ENV_NAME", "wrong")
    instance, _, _, _ = gate.registry_inputs(config)
    assert instance["sourceAgentId"] == "azure-agents-control-plane:unit:next-best-action"
    assert instance["agentIdentityBlueprintId"] == OBJECT
    assert instance["managedByAppId"] == OWNER


def test_named_azd_config_does_not_merge_stale_flags_or_export_secrets(monkeypatch):
    raw = values()
    raw.update(LOGIC_APP_APPROVAL_WEBHOOK="SECRET", AZURE_CLIENT_SECRET="SECRET")
    raw["APPROVAL_RUNTIME_CONFIG"] = {"APPROVAL_CALLBACK_AUDIENCE": "api://" + OBJECT, "LOGIC_APP_APPROVAL_WEBHOOK": "SECRET"}
    monkeypatch.setenv("AGENT_IDENTITY_ENABLED", "false")
    gate.run_cli.return_value = json.dumps(raw)
    gate.run_cli.side_effect = None
    config = gate.load_configuration(from_azd=True, environment="unit")
    assert config["AGENT_IDENTITY_ENABLED"] == "true"
    assert "SECRET" not in json.dumps(config)
    assert "--environment" in gate.run_cli.call_args.args[0]
    assert config["AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID"] == ""


def test_azd_environment_mismatch_blocks():
    gate.run_cli.side_effect = None
    gate.run_cli.return_value = json.dumps({"AZURE_ENV_NAME": "other"})
    with pytest.raises(gate.RegistryError, match="different environment"):
        gate.load_configuration(from_azd=True, environment="unit")


def test_gate_cli_returns_nonzero_and_never_publishes_on_failed_check(monkeypatch, capsys):
    monkeypatch.setattr(gate, "load_configuration", Mock(return_value=values()))
    monkeypatch.setattr(gate, "check_deployment", Mock(side_effect=gate.GateBlocked({"status": "blocked", "deploymentReady": False})))
    publisher = Mock()
    monkeypatch.setattr(gate, "AgentRegistryPublisher", publisher)
    assert gate.main(["--publish-registry"]) == 2
    assert json.loads(capsys.readouterr().out)["deploymentReady"] is False
    publisher.assert_not_called()


def test_build_cli_cannot_override_azd_identity_flags(monkeypatch, capsys):
    monkeypatch.setattr(build, "load_configuration", Mock(return_value=values()))
    monkeypatch.setenv("AGENT_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENT_REGISTRY_ENABLED", "false")
    invoked = Mock(side_effect=gate.GateBlocked({"status": "blocked"}))
    monkeypatch.setattr(build, "build_and_push", invoked)
    assert build.main(["--environment=unit", "--check-only"]) == 2
    assert invoked.call_args.args[0]["AGENT_IDENTITY_ENABLED"] == "true"
    assert invoked.call_args.args[0]["AGENT_REGISTRY_ENABLED"] == "true"
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize("name", ["AGENT_IDENTITY_ENABLED", "AGENT_REGISTRY_ENABLED", "APPROVAL_LOGIC_APP_ENABLED"])
def test_explicit_empty_flags_rejected_before_shell_export(name):
    raw = values()
    raw[name] = ""
    gate.run_cli.side_effect = None
    gate.run_cli.return_value = json.dumps(raw)
    with pytest.raises(gate.RegistryError, match=name):
        gate.load_configuration(from_azd=True, environment="unit")


def test_gate_rejects_missing_registry_consent_check(monkeypatch, tmp_path):
    config = configured_gate(monkeypatch, tmp_path)
    gate.identity_preflight.return_value["checks"].pop()
    with pytest.raises(gate.RegistryError, match="Registry consent"):
        gate.check_deployment(config)


def test_gate_validates_saved_registration_binding(monkeypatch, tmp_path):
    config = configured_gate(monkeypatch, tmp_path)
    config["AGENT_REGISTRY_ID"] = "saved-registration"
    publisher = Mock()
    publisher.verify_existing_binding.side_effect = gate.RegistryError("identity_mismatch", "Existing registration belongs to another agent")
    monkeypatch.setattr(gate, "AgentRegistryPublisher", Mock(return_value=publisher))
    with pytest.raises(gate.RegistryError, match="another agent"):
        gate.check_deployment(config)
    publisher.verify_existing_binding.assert_called_once()
    publisher.publish.assert_not_called()


def test_target_resolution_pins_subscription_tenant_and_uami():
    config = values()
    gate.run_cli.side_effect = [
        json.dumps({"id": SUBSCRIPTION, "tenantId": TENANT, "state": "Enabled"}),
        json.dumps([{ "id": RESOURCE, "name": "id-mcp-unit", "clientId": MI_CLIENT, "principalId": MI }]),
    ]
    assert gate.resolve_target(config) == target()
    assert all("--subscription" in call.args[0] for call in gate.run_cli.call_args_list)


def test_target_wrong_tenant_stops_before_identity_lookup():
    gate.run_cli.side_effect = None
    gate.run_cli.return_value = json.dumps({"id": SUBSCRIPTION, "tenantId": OWNER, "state": "Enabled"})
    with pytest.raises(gate.RegistryError, match="different tenant"):
        gate.resolve_target(values())
    assert gate.run_cli.call_count == 1


def args(**changes):
    return SimpleNamespace(**dict({"environment": "unit", "phase": "blueprint", "apply": False,
                                  "import_outputs": False, "blueprint_app_id": None,
                                  "agent_identity_id": None}, **changes))


def stage_config():
    config = values()
    for key in ("AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_PRINCIPAL_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID",
                "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID", "AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID"):
        config[key] = ""
    return config


def test_stage_default_check_never_provisions_or_exports(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    monkeypatch.setattr(stage, "preflight", Mock(return_value={"status": "satisfied", "deploymentReady": False}))
    result = stage.deploy(stage_config(), args())
    assert result["readOnly"] is True and result["deploymentReady"] is False
    stage.run_cli.assert_not_called()


def test_stage_apply_blocked_preflight_never_submits_deployment(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    monkeypatch.setattr(stage, "preflight", Mock(side_effect=gate.GateBlocked({"status": "blocked"})))
    with pytest.raises(gate.GateBlocked):
        stage.deploy(stage_config(), args(apply=True))
    stage.run_cli.assert_not_called()


def test_child_phase_requires_preconsented_blueprint_before_external_calls():
    with pytest.raises(gate.RegistryError, match="precreated"):
        stage.deploy(stage_config(), args(phase="identity", apply=True))
    stage.run_cli.assert_not_called()


def outputs(phase="identity"):
    output = {key: values().get(key) for key in stage.IDENTITY_OUTPUTS}
    output["AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID"] = RESOURCE
    output["AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID"] = MI
    if phase == "blueprint":
        for key in ("AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_PRINCIPAL_ID", "AGENT_IDENTITY_DISPLAY_NAME"):
            output[key] = ""
    return output


def deployment(phase="identity"):
    return {"properties": {"provisioningState": "Succeeded", "outputs": {
        key: {"type": "String", "value": value} for key, value in outputs(phase).items()
    }}}


def test_stage_uses_only_scoped_incremental_template_and_verified_handoff(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    monkeypatch.setattr(stage, "preflight", Mock(return_value=report()))
    monkeypatch.setattr(stage, "verified_outputs", Mock(return_value=outputs()))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[:4] == ["az", "deployment", "group", "create"]:
            assert command[command.index("--mode") + 1] == "Incremental"
            assert command[command.index("--template-file") + 1] == str(ROOT / "infra/agent-identity.bicep")
            path = Path(command[command.index("--parameters") + 1][1:])
            params = json.loads(path.read_text())["parameters"]
            assert params["existingBlueprintAppId"]["value"] == BLUEPRINT
            assert params["existingAgentIdentityId"]["value"] == AGENT
            return json.dumps(deployment())
        assert command[:3] == ["azd", "env", "set"]
        assert stage.verified_outputs.called
        assert command[-2:] == ["--environment", "unit"]
        return ""
    stage.run_cli.side_effect = run
    result = stage.deploy(values(), args(phase="identity", apply=True))
    assert result["runtimeDeployed"] is False
    assert any("EXISTING_AGENT_IDENTITY_ID" in call for call in calls)
    assert not any("AGENT_IDENTITY_ENABLED" in call for call in calls)


def test_recovery_import_never_replays_create(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    monkeypatch.setattr(stage, "preflight", Mock(return_value=report()))
    monkeypatch.setattr(stage, "verified_outputs", Mock(return_value=outputs("blueprint")))
    stage.run_cli.side_effect = lambda command, **kwargs: json.dumps(deployment("blueprint")) if command[:4] == ["az", "deployment", "group", "show"] else ""
    stage.deploy(stage_config(), args(import_outputs=True))
    assert not any("create" in call.args[0] for call in stage.run_cli.call_args_list)


def test_identity_recovery_can_verify_when_azd_export_never_started(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    monkeypatch.setattr(stage, "preflight", Mock(return_value=report()))
    stage.run_cli.side_effect = lambda command, **kwargs: json.dumps(deployment()) if command[:4] == ["az", "deployment", "group", "show"] else ""
    result = stage.deploy(stage_config(), args(phase="identity", import_outputs=True))
    assert result["outputs"]["AGENT_IDENTITY_APP_ID"] == AGENT
    assert stage.preflight.call_args.args[-2:] == (BLUEPRINT, AGENT)
    assert not any("create" in call.args[0] for call in stage.run_cli.call_args_list)


def test_recovery_bad_output_or_failed_readback_never_exports(monkeypatch):
    monkeypatch.setattr(stage, "resolve_target", Mock(return_value=target()))
    stage.run_cli.side_effect = None
    stage.run_cli.return_value = json.dumps(deployment())
    monkeypatch.setattr(stage, "preflight", Mock(side_effect=gate.GateBlocked({"status": "blocked"})))
    with pytest.raises(gate.GateBlocked):
        stage.deploy(stage_config(), args(phase="identity", import_outputs=True))
    stage.run_cli.assert_called_once()


def test_blueprint_readback_can_export_without_child_consent(monkeypatch):
    ready = report()
    ready["deploymentReady"] = False
    ready["checks"][1]["status"] = "missing_permissions"
    monkeypatch.setattr(stage, "preflight", Mock(return_value=ready))
    result = stage.verified_outputs(deployment("blueprint"), stage_config(), target(), "blueprint")
    assert result["AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID"] == PRINCIPAL
    assert "AGENT_IDENTITY_APP_ID" not in result


@pytest.mark.parametrize("field,value", [("AGENT_IDENTITY_APP_ID", ""), ("AGENT_IDENTITY_PRINCIPAL_ID", MI), ("AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID", AGENT)])
def test_invalid_output_blocks_handoff_before_graph(field, value):
    result = deployment()
    result["properties"]["outputs"][field]["value"] = value
    with pytest.raises(gate.RegistryError):
        stage.verified_outputs(result, values(), target(), "identity")
    stage.GraphClient.assert_not_called()


def registry_record(**changes):
    return {"id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-unit/providers/Microsoft.ContainerRegistry/registries/registryunit",
            "loginServer": "registryunit.azurecr.io", "publicNetworkAccess": "Disabled",
            "networkRuleSet": {"defaultAction": "Deny"}, "networkRuleBypassOptions": "None", **changes}


def pool_record(**changes):
    return {"provisioningState": "Succeeded", "os": "Linux", "count": 1,
            "virtualNetworkSubnetResourceId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-unit/providers/Microsoft.Network/virtualNetworks/unit/subnets/build", **changes}


def test_build_gate_failure_cannot_authenticate_or_queue(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock(side_effect=gate.GateBlocked({"status": "blocked"})))
    with pytest.raises(gate.GateBlocked):
        build.build_and_push(values())
    build.run_cli.assert_not_called()


def test_private_acr_without_pool_fails_without_network_changes(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock())
    build.run_cli.side_effect = None
    build.run_cli.return_value = json.dumps(registry_record())
    config = values()
    config["ACR_TASK_AGENT_POOL"] = ""
    with pytest.raises(gate.RegistryError, match="Private/restricted"):
        build.build_and_push(config)
    assert build.run_cli.call_count == 1
    assert build.run_cli.call_args.args[0][:3] == ["az", "acr", "show"]


@pytest.mark.parametrize("change", [{"count": 0}, {"count": True}, {"os": "Windows"}, {"provisioningState": "Failed"}, {"virtualNetworkSubnetResourceId": ""}])
def test_unready_private_pool_never_queues(monkeypatch, change):
    monkeypatch.setattr(build, "check_deployment", Mock())
    build.run_cli.side_effect = [json.dumps(registry_record()), json.dumps(pool_record(**change))]
    with pytest.raises(gate.RegistryError):
        build.build_and_push(values())
    assert all("build" not in call.args[0] for call in build.run_cli.call_args_list)


def test_private_pool_build_checks_policy_after_failure(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock())
    seen = []
    def run(command, **kwargs):
        seen.append(command)
        if command[:3] == ["az", "acr", "show"]:
            return json.dumps(registry_record())
        if command[:3] == ["az", "acr", "agentpool"]:
            return json.dumps(pool_record())
        assert command[:3] == ["az", "acr", "build"]
        assert command[command.index("--agent-pool") + 1] == "private-pool"
        assert command[command.index("--platform") + 1] == "linux/amd64"
        raise gate.RegistryError("command_failed", "Fake build failed")
    build.run_cli.side_effect = run
    with pytest.raises(gate.RegistryError, match="Fake build"):
        build.build_and_push(values())
    assert sum(call[:3] == ["az", "acr", "show"] for call in seen) == 2
    assert not any("update" in call or "network-rule" in call for call in seen)


def test_private_docker_runner_build_returns_verified_digest(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock())
    config = dict(values(), ACR_BUILD_MODE="docker")
    build.run_cli.side_effect = lambda command, **kwargs: json.dumps(registry_record()) if command[:3] == ["az", "acr", "show"] else DIGEST if "repository" in command else ""
    result = build.build_and_push(config)
    assert result["immutableImage"].endswith("@" + DIGEST)
    assert result["networkPolicyUnchanged"] is True
    calls = [call.args[0] for call in build.run_cli.call_args_list]
    assert any(call[:2] == ["docker", "build"] for call in calls)
    assert any(call[:2] == ["docker", "push"] for call in calls)
    assert not any("--expose-token" in call or "update" in call for call in calls)


def test_network_drift_stops_rollout_without_restoring_policy(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock())
    records = iter([registry_record(), registry_record(publicNetworkAccess="Enabled")])
    build.run_cli.side_effect = lambda command, **kwargs: json.dumps(next(records)) if command[:3] == ["az", "acr", "show"] else DIGEST if "repository" in command else ""
    with pytest.raises(gate.RegistryError, match="policy changed"):
        build.build_and_push(dict(values(), ACR_BUILD_MODE="docker"))


def test_build_check_only_is_read_only(monkeypatch):
    monkeypatch.setattr(build, "check_deployment", Mock())
    build.run_cli.side_effect = [json.dumps(registry_record()), json.dumps(pool_record())]
    assert build.build_and_push(values(), check_only=True)["readOnly"] is True
    assert build.run_cli.call_count == 2


def test_scoped_template_has_no_workload_or_approval_resource_writes():
    source = (ROOT / "infra/agent-identity.bicep").read_text()
    assert "targetScope = 'resourceGroup'" in source
    assert "existing = {" in source
    for forbidden in ("Microsoft.ContainerService", "Microsoft.ContainerRegistry", "Microsoft.Logic", "Microsoft.Web/connections", "sqlDatabases", "containers", "approvals.bicep", "aksFederatedCredential"):
        assert forbidden not in source
    assert "if (phase == 'identity')" in source
    assert "blueprintPrincipalId" in source
    assert "blueprintObjectId" in source


def test_hooks_gate_before_mutations_and_repeat_before_rollout():
    for name in ("infra/hooks/postprovision.ps1", "infra/hooks/postprovision.sh"):
        source = (ROOT / name).read_text(encoding="utf-8")
        first = source.index("deployment_gate.py' --check") if name.endswith("ps1") else source.index("deployment_gate.py --check")
        assert first < source.index("az aks get-credentials")
        build_at = source.index("./scripts/build-and-push.")
        rollout_gate = source.index("deployment_gate.py", build_at)
        assert build_at < rollout_gate < source.index("# Inject approval runtime")
        assert source.index("--publish-registry") > source.index("kubectl rollout status")
        assert "eval $(" not in source
        assert "immutableImage" in source and "digest" in source


def test_build_wrappers_cannot_mutate_firewalls():
    for name in ("scripts/build-and-push.ps1", "scripts/build-and-push.sh", "scripts/build_and_push.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("az acr update", "--public-network-enabled", "--default-action", "--allow-trusted-services", "network-rule add"):
            assert forbidden not in source


def test_identity_role_module_includes_foundry_agent_service_role():
    source = (ROOT / "infra/app/agent-RoleAssignments.bicep").read_text()
    assert "64702f94-c441-49e6-a78b-ef80e0188fee" in source
    assert "resource foundryAIDeveloperAgent" in source