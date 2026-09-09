"""Offline parent/template contracts and mocked approval-injection CLI tests.

Reads only named non-secret source files. All subprocess execution and command
discovery are mocked; no application imports, env files, credentials, network,
builds or deployed resources are used. Source checks do not replace Bicep/ARM,
shell syntax, actual Kubernetes/Teams integration or runtime fail-closed tests.
"""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/configure_approval_runtime.py"
SPEC = importlib.util.spec_from_file_location("approval_runtime_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
TENANT = "aaaaaaaa-2222-3333-4444-555555555555"
APP = "bbbbbbbb-2222-3333-4444-555555555555"
PRINCIPAL = "cccccccc-2222-3333-4444-555555555555"
APPROVER = "dddddddd-2222-3333-4444-555555555555"
CALLBACK = "https://approval-gateway.azure-api.net/agent-approvals/callback"
SENTINEL = "FAKE_SENSITIVE_TEST_VALUE_NEVER_LOG"
WEBHOOK = (
    "https://prod-00.eastus.logic.azure.com:443/workflows/workflow-id/"
    "triggers/When_an_HTTP_request_is_received/paths/invoke?api-version=2016-10-01&sig=" + SENTINEL
)


def valid_configuration() -> dict[str, str]:
    return {
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_RESOURCE_GROUP_NAME": "rg-test",
        "APPROVAL_LOGIC_APP_NAME": "logic-approval-test",
        "K8S_NAMESPACE": "mcp-agents",
        "AZURE_TENANT_ID": TENANT,
        "APPROVAL_APPROVER_TENANT_ID": TENANT,
        "APPROVAL_CALLBACK_AUDIENCE": f"api://{APP}",
        "APPROVAL_CALLBACK_PRINCIPAL_ID": PRINCIPAL,
        "APPROVAL_APPROVER_IDS": APPROVER,
        "COSMOSDB_APPROVALS_CONTAINER": "approvals",
        "APPROVAL_TIMEOUT_HOURS": "2",
        "APPROVAL_CALLBACK_URL": CALLBACK,
        "DEPLOYMENT_ENVIRONMENT": "test",
        "AKS_CLUSTER_NAME": "aks-test",
    }


def module_block(source: str, symbol: str) -> str:
    match = re.search(rf"(?ms)^module {re.escape(symbol)} '[^']+'[^\n]*\n.*?^\}}", source)
    if match is None:
        raise AssertionError(f"Missing module: {symbol}")
    return match.group(0)


class DeploymentSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        cls.parameters = json.loads((ROOT / "infra/main.parameters.json").read_text(encoding="utf-8"))["parameters"]
        cls.template = (ROOT / "k8s/mcp-agents-deployment.yaml").read_text(encoding="utf-8")
        cls.ps = (ROOT / "infra/hooks/postprovision.ps1").read_text(encoding="utf-8")
        cls.sh = (ROOT / "infra/hooks/postprovision.sh").read_text(encoding="utf-8")
        cls.script = SCRIPT.read_text(encoding="utf-8")

    def test_parent_parameter_names_match_actual_children(self) -> None:
        for symbol in ("agentsApprovalLogicApp", "apimApprovalCallback", "agentIdentityBlueprint", "agentFederatedCredential"):
            with self.subTest(module=symbol):
                block = module_block(self.main, symbol)
                path_match = re.search(r"^module \w+ '([^']+)'", block)
                assert path_match is not None
                child = (ROOT / "infra" / path_match.group(1)).read_text(encoding="utf-8")
                declarations = dict(re.findall(r"(?m)^param (\w+) ([^\n]+)", child))
                supplied = set(re.findall(r"(?m)^    (\w+):", block))
                self.assertLessEqual(supplied, set(declarations))
                self.assertLessEqual({name for name, declaration in declarations.items() if "=" not in declaration}, supplied)
                if symbol in ("agentsApprovalLogicApp", "apimApprovalCallback"):
                    self.assertEqual(supplied, set(declarations))
        blueprint = module_block(self.main, "agentIdentityBlueprint")
        self.assertIn("managedIdentityResourceId: mcpUserAssignedIdentity.outputs.identityId", blueprint)
        self.assertIn("existingBlueprintAppId: existingAgentIdentityBlueprintAppId", blueprint)
        self.assertNotIn("federatedIdentityClientId:", blueprint)
        identity = module_block(self.main, "nextBestActionAgentIdentity")
        self.assertIn("existingAgentIdentityId: existingAgentIdentityId", identity)
        roles = module_block(self.main, "agentRoleAssignments")
        self.assertIn("searchEnabled: searchEnabled", roles)
        role_module = (ROOT / "infra/app/agent-RoleAssignments.bicep").read_text(encoding="utf-8")
        self.assertIn("resource searchService", role_module)
        self.assertIn("existing = if (searchEnabled)", role_module)
        self.assertIn("resource searchIndexDataContributorAgent", role_module)
        self.assertIn("= if (searchEnabled)", role_module)
        federation = module_block(self.main, "agentFederatedCredential")
        self.assertIn("blueprintAppId: agentIdentityBlueprint!.outputs.blueprintAppId", federation)
        self.assertIn("blueprintObjectId: agentIdentityBlueprint!.outputs.blueprintObjectId", federation)
        self.assertNotIn("nextBestActionAgentIdentity", federation)
        self.assertNotRegex(federation, r"\bidentity(Client|Principal)Id:")

    def test_callback_dependency_is_one_way_and_database_precedes_container(self) -> None:
        logic = module_block(self.main, "agentsApprovalLogicApp")
        callback = module_block(self.main, "apimApprovalCallback")
        self.assertIn("var approvalCallbackUrl = '${apimService.outputs.gatewayUrl}/agent-approvals/callback'", self.main)
        self.assertNotIn("apimApprovalCallback", logic)
        self.assertNotIn("apimApprovalCallback.outputs", self.main)
        self.assertNotIn("apimApprovalCallback!.outputs", self.main)
        self.assertIn("callbackUrl: approvalCallbackUrl", logic)
        self.assertIn("callbackAudience: approvalCallbackAudience", logic)
        self.assertIn("cosmosDbDatabaseName: cosmosDatabase.outputs.name", logic)
        self.assertRegex(logic, r"dependsOn:\s*\[\s*cosmosDatabase\s*\]")
        self.assertNotIn("userAssignedIdentityId", logic)
        for expected in (
            "if (approvalLogicAppEnabled)", "apimServiceName: apimService.outputs.name",
            "backendUrl: 'http://10.0.4.4'", "tenantId: tenant().tenantId",
            "callbackAudience: approvalCallbackAudience",
            "logicAppPrincipalId: agentsApprovalLogicApp!.outputs.logicAppPrincipalId",
        ):
            self.assertIn(expected, callback)

    def test_explicit_approvers_audience_and_existing_identity_switches(self) -> None:
        self.assertIn("var approvalCallbackAudience = !empty(trim(existingApprovalCallbackAppUri))\n  ? toLower(trim(existingApprovalCallbackAppUri))\n  : (agentIdentityEnabled ? 'api://${agentIdentityBlueprint!.outputs.blueprintAppId}' : '')", self.main)
        self.assertIn("param approvalApproverIds string = ''", self.main)
        self.assertIn("map(split(approvalApproverIds, ','), id => toLower(trim(id)))", self.main)
        self.assertIn("approverIds: approvalApproverIdList", module_block(self.main, "agentsApprovalLogicApp"))
        approval_section = self.main.split("var approvalCallbackUrl =", 1)[1].split("// Microsoft Defender for Cloud", 1)[0]
        self.assertNotIn("agentSponsorPrincipalId", approval_section)
        self.assertRegex(self.main, r"@minValue\(1\)\s*@maxValue\(24\)\s*param approvalTimeoutHours int = 2")
        for parameter, value in {
            "approvalLogicAppEnabled": "${APPROVAL_LOGIC_APP_ENABLED=false}",
            "existingApprovalCallbackAppUri": "${EXISTING_APPROVAL_CALLBACK_APP_URI=}",
            "approvalApproverIds": "${APPROVAL_APPROVER_IDS=}",
            "approvalTimeoutHours": "${APPROVAL_TIMEOUT_HOURS=2}",
            "teamsChannelId": "${TEAMS_CHANNEL_ID=}",
            "teamsGroupId": "${TEAMS_GROUP_ID=}",
            "agentIdentityEnabled": "${AGENT_IDENTITY_ENABLED=false}",
            "agentSponsorPrincipalId": "${AGENT_SPONSOR_PRINCIPAL_ID=}",
            "existingAgentIdentityBlueprintAppId": "${EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID=}",
            "existingAgentIdentityId": "${EXISTING_AGENT_IDENTITY_ID=}",
        }.items():
            self.assertEqual(self.parameters[parameter]["value"], value)
        self.assertIn("param agentIdentityEnabled bool = true", self.main)
        self.assertIn("param approvalLogicAppEnabled bool = false", self.main)

    def test_safe_outputs_never_promote_the_signed_trigger(self) -> None:
        self.assertNotIn("APPROVAL_LOGIC_APP_TRIGGER_URL", self.main)
        self.assertNotIn("logicAppTriggerUrl", self.main)
        self.assertNotIn("listCallbackUrl(", self.main)
        outputs = dict(re.findall(r"(?m)^output (\w+) \w+ = (.+)$", self.main))
        self.assertLessEqual(len(outputs), 64, "ARM has a hard limit of 64 outputs")
        self.assertIn("APPROVAL_RUNTIME_CONFIG", outputs)
        grouped = self.main.split("output APPROVAL_RUNTIME_CONFIG object = {", 1)[1].split("\n}", 1)[0]
        outputs.update(dict(re.findall(r"(?m)^  (\w+): (.+)$", grouped)))
        for name in (
            "APPROVAL_LOGIC_APP_RESOURCE_ID", "APPROVAL_LOGIC_APP_NAME",
            *runtime.RUNTIME_KEYS,
        ):
            self.assertIn(name, outputs)
        self.assertIn("agentsApprovalLogicApp!.outputs.logicAppId", outputs["APPROVAL_LOGIC_APP_RESOURCE_ID"])
        self.assertIn("agentsApprovalLogicApp!.outputs.logicAppPrincipalId", outputs["APPROVAL_CALLBACK_PRINCIPAL_ID"])
        self.assertIn("agentsApprovalLogicApp!.outputs.approvalsContainerName", outputs["COSMOSDB_APPROVALS_CONTAINER"])
        self.assertEqual(outputs["APPROVAL_APPROVER_IDS"], "join(approvalApproverIdList, ',')")
        self.assertEqual(outputs["APPROVAL_TIMEOUT_HOURS"], "approvalTimeoutHours")
        self.assertEqual(outputs["DEPLOYMENT_ENVIRONMENT"], "environmentName")
        self.assertIn("AGENT_IDENTITY_BLUEPRINT_OBJECT_ID", outputs)

    def test_template_bootstrap_identity_optional_refs_and_unique_environment(self) -> None:
        self.assertIn("serviceAccountName: mcp-agents-sa", self.template)
        for document in self.template.split("\n---\n"):
            if "kind: ServiceAccount\n" in document:
                self.assertIn('azure.workload.identity/client-id: "${MCP_SERVER_IDENTITY_CLIENT_ID}"', document)
                self.assertNotIn("${AGENT_IDENTITY_APP_ID}", document)
        self.assertIn('entra.agent-id/enabled: "${AGENT_IDENTITY_ENABLED}"', self.template)
        for reference, name in (("configMapRef", "mcp-approval-config"), ("secretRef", "mcp-approval-secrets")):
            self.assertRegex(self.template, rf"{reference}:\s+name: {name}\s+optional: true")
        env_block = self.template.split("        env:\n", 1)[1].split("        volumeMounts:", 1)[0]
        entries = re.findall(r"(?ms)^        - name: (\w+)\n(.*?)(?=^        - name:|\Z)", env_block)
        env = dict(entries)
        self.assertEqual(len(entries), len(env), "Duplicate explicit environment variable")
        for name, value in {
            "AZURE_CLIENT_ID": "${MCP_SERVER_IDENTITY_CLIENT_ID}",
            "AGENT_IDENTITY_APP_ID": "${AGENT_IDENTITY_APP_ID}",
            "AGENT_IDENTITY_BLUEPRINT_APP_ID": "${AGENT_IDENTITY_BLUEPRINT_APP_ID}",
            "AGENT_IDENTITY_ENABLED": "${AGENT_IDENTITY_ENABLED}",
            "IMAGE_TAG": "${IMAGE_TAG}", "COMMIT_SHA": "${COMMIT_SHA}",
            "FOUNDRY_MODEL_DEPLOYMENT_NAME": "${FOUNDRY_MODEL_DEPLOYMENT_NAME}",
            "EMBEDDING_MODEL_DEPLOYMENT_NAME": "${EMBEDDING_MODEL_DEPLOYMENT_NAME}",
            "AGENT_LEARNING_STORE_BACKEND": "${AGENT_LEARNING_STORE_BACKEND:-cosmos}",
            "AGENT_LEARNING_ENABLE_CAPTURE": "${AGENT_LEARNING_ENABLE_CAPTURE:-false}",
        }.items():
            self.assertIn(f'value: "{value}"', env[name])
        self.assertIn("fieldPath: metadata.namespace", env["K8S_NAMESPACE"])
        self.assertFalse(set(runtime.RUNTIME_KEYS) & set(env), "ConfigMap settings must not be duplicated/shadowed")
        self.assertNotIn("LOGIC_APP_APPROVAL_WEBHOOK", env)
        self.assertNotIn("APPROVAL_LOGIC_APP_TRIGGER_URL", self.template)

    def test_hooks_use_actual_ids_and_inject_before_deployment(self) -> None:
        for expected in (
            r"-replace '\$\{AGENT_IDENTITY_APP_ID\}', $agentIdentityAppId",
            r"-replace '\$\{AGENT_IDENTITY_BLUEPRINT_APP_ID\}', $agentBlueprintAppId",
            r"-replace '\$\{AGENT_IDENTITY_DISPLAY_NAME\}', $agentIdentityDisplayName",
            r"-replace '\$\{AGENT_IDENTITY_ENABLED\}', $agentIdentityEnabled",
            r"-replace '\$\{MCP_SERVER_IDENTITY_CLIENT_ID\}', $mcpIdentityClientId",
            r"-replace '\$\{COMMIT_SHA\}', $commitSha",
        ):
            self.assertIn(expected, self.ps)
        for expected in (
            r"\${AGENT_IDENTITY_APP_ID}|$AGENT_APP_ID|g",
            r"\${AGENT_IDENTITY_BLUEPRINT_APP_ID}|$AGENT_BLUEPRINT_APP_ID|g",
            r"\${AGENT_IDENTITY_DISPLAY_NAME}|$AGENT_DISPLAY_NAME|g",
            r"\${AGENT_IDENTITY_ENABLED}|$AGENT_IDENTITY_FLAG|g",
            r"\${MCP_SERVER_IDENTITY_CLIENT_ID}|$MCP_IDENTITY_CLIENT_ID|g",
            r"\${COMMIT_SHA}|$COMMIT_VALUE|g",
        ):
            self.assertIn(expected, self.sh)
        for source, guard in (
            (self.ps, "if ($approvalLogicAppEnabled -eq 'true')"),
            (self.sh, "if [ \"$APPROVAL_ENABLED\" = 'true' ]; then"),
        ):
            block = source.split("# Inject approval runtime ", 1)[1].split("kubectl apply -f ./k8s/mcp-agents-deployment-configured.yaml", 1)[0]
            self.assertIn(guard, block)
            self.assertLess(block.index('"kind":"Namespace"'), block.index("scripts/configure_approval_runtime.py"))
            self.assertIn("--apply", block)
            self.assertIn("--from-azd", block)
            self.assertIn("--namespace=mcp-agents", block)
            for option in ("subscription-id", "resource-group", "namespace", "tenant-id", "environment", "cluster-name"):
                self.assertIn(f"--{option}=", block)
            self.assertNotIn("--callback-principal-id=", block, "Grouped generated values must not be shadowed by stale flat outputs")
            self.assertNotIn("Out-File", block)
            self.assertNotIn("APPROVAL_LOGIC_APP_TRIGGER_URL", source)
            self.assertIn("Python 3.10+", source)
            self.assertLess(source.index("scripts/configure_approval_runtime.py"), source.index("kubectl rollout restart"))

    def test_registry_publication_is_opt_in_linked_and_after_rollout(self) -> None:
        for source in (self.ps, self.sh):
            self.assertIn("AGENT_REGISTRY_ENABLED", source)
            self.assertIn("scripts/publish_agent_registry.py", source)
            self.assertIn("--blueprint-object-id=", source)
            self.assertIn("--agent-identity-id=", source)
            self.assertIn("--owner-id=", source)
            self.assertLess(source.index("kubectl rollout status"), source.index("scripts/publish_agent_registry.py"))
            self.assertNotIn("LOGIC_APP_APPROVAL_WEBHOOK", source)

    def test_injector_does_not_write_files_or_change_identity_and_permissions(self) -> None:
        self.assertNotRegex(self.script, r"\b(?:open|write_text|write_bytes)\s*\(")
        self.assertNotIn("tempfile", self.script)
        self.assertNotIn("--from-literal", self.script)
        self.assertNotRegex(self.script, r"(?:az login|azd auth|role assignment|appRoleAssignments)")
        for name in ("AGENT_IDENTITY_ENABLED", "AGENT_IDENTITY_APP_ID", "AZURE_CLIENT_ID"):
            self.assertNotIn(name, runtime.RUNTIME_KEYS)


class RuntimeInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Use addCleanup rather than TestCase.enterContext (Python 3.11+) so
        # these tests share the CLI's Python 3.10 minimum.
        environment = patch.dict(runtime.os.environ, valid_configuration(), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        discovery = patch.object(runtime.shutil, "which", side_effect=lambda name: name)
        discovery.start()
        self.addCleanup(discovery.stop)
        commands = patch.object(runtime.subprocess, "run")
        self.commands = commands.start()
        self.addCleanup(commands.stop)
        self.commands.side_effect = AssertionError("Unexpected external command; tests must remain offline")

    def invoke(self, *args: str) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = runtime.main(list(args))
        return code, stdout.getvalue() + stderr.getvalue()

    @staticmethod
    def response(stdout: str, code: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], code, stdout=stdout, stderr=SENTINEL)

    def test_normalizes_ids_and_keeps_an_explicit_comma_allowlist(self) -> None:
        values = valid_configuration()
        values["AZURE_TENANT_ID"] = TENANT.upper()
        values["APPROVAL_CALLBACK_AUDIENCE"] = f"api://{APP.upper()}"
        values["APPROVAL_APPROVER_IDS"] = f" {APPROVER.upper()}, {PRINCIPAL} "
        values["AGENT_IDENTITY_ENABLED"] = "true"
        result = runtime.validate_configuration(values)
        self.assertEqual(result["AZURE_TENANT_ID"], TENANT)
        self.assertEqual(result["APPROVAL_CALLBACK_AUDIENCE"], f"api://{APP}")
        self.assertEqual(result["APPROVAL_APPROVER_IDS"], f"{APPROVER},{PRINCIPAL}")
        self.assertEqual(set(result), set(runtime.REQUIRED_KEYS))
        self.commands.assert_not_called()

    def test_reports_all_missing_names_without_values(self) -> None:
        with self.assertRaises(runtime.ConfigurationError) as failure:
            runtime.validate_configuration({})
        for name in runtime.REQUIRED_KEYS:
            self.assertIn(name, str(failure.exception))
        self.assertNotIn(SENTINEL, str(failure.exception))

    def test_missing_any_required_setting_blocks_apply_before_external_calls(self) -> None:
        for name in runtime.REQUIRED_KEYS:
            with self.subTest(setting=name), patch.dict(runtime.os.environ, {name: ""}):
                code, output = self.invoke("--apply")
                self.assertEqual(code, 2)
                self.assertIn(name, output)
                self.commands.assert_not_called()

    def test_rejects_bad_guids_allowlists_timeout_and_context_without_logging_values(self) -> None:
        cases = {
            "AZURE_TENANT_ID": [SENTINEL, "00000000-0000-0000-0000-000000000000"],
            "AZURE_SUBSCRIPTION_ID": [SENTINEL],
            "APPROVAL_CALLBACK_PRINCIPAL_ID": [SENTINEL],
            "APPROVAL_CALLBACK_AUDIENCE": [APP, "api://" + SENTINEL, f"api://{APP}/scope"],
            "APPROVAL_APPROVER_IDS": [SENTINEL, f"{APPROVER},", f",{APPROVER}", f"{APPROVER},,{PRINCIPAL}", json.dumps([APPROVER]), ",".join([APPROVER] * 101)],
            "APPROVAL_TIMEOUT_HOURS": ["0", "25", "-1", "2.5", "nan", "inf"],
            "K8S_NAMESPACE": ["../other", "Uppercase", "-bad", "x" * 64],
            "AZURE_RESOURCE_GROUP_NAME": ["../other", "name."],
            "APPROVAL_LOGIC_APP_NAME": ["../other", "x" * 81],
            "COSMOSDB_APPROVALS_CONTAINER": ["bad/name"],
            "DEPLOYMENT_ENVIRONMENT": ["bad/environment"],
            "AKS_CLUSTER_NAME": ["bad cluster"],
        }
        for name, candidates in cases.items():
            for value in candidates:
                with self.subTest(setting=name, value=value), patch.dict(runtime.os.environ, {name: value}):
                    code, output = self.invoke("--apply")
                    self.assertEqual(code, 2)
                    self.assertIn(name, output)
                    self.assertNotIn(SENTINEL, output)
                    self.commands.assert_not_called()
        for timeout in ("1", "24"):
            self.assertEqual(runtime.validate_configuration({**valid_configuration(), "APPROVAL_TIMEOUT_HOURS": timeout})["APPROVAL_TIMEOUT_HOURS"], timeout)

    def test_callback_requires_exact_https_path_and_no_secret_query(self) -> None:
        for url in (
            CALLBACK.replace("https:", "http:"), CALLBACK + "?sig=" + SENTINEL,
            CALLBACK + "?", CALLBACK + "#", CALLBACK + "/extra", WEBHOOK,
            CALLBACK.replace("/callback", "/other"), CALLBACK.replace("https://", "https://user:password@"),
            CALLBACK.replace(".net/", ".net:8443/"), CALLBACK.replace("/callback", "/%63allback"),
            CALLBACK.replace("gateway", "gate\nway"), CALLBACK.replace(".net/", ".net\\/"),
        ):
            with self.subTest(url=url), patch.dict(runtime.os.environ, {"APPROVAL_CALLBACK_URL": url}):
                code, output = self.invoke("--apply")
                self.assertEqual(code, 2)
                self.assertIn("APPROVAL_CALLBACK_URL", output)
                self.assertNotIn(SENTINEL, output)
                self.commands.assert_not_called()
        runtime.validate_configuration({**valid_configuration(), "APPROVAL_CALLBACK_URL": CALLBACK.replace(".net/", ".net:443/")})

    def test_check_only_is_offline_and_uses_namespace_and_environment_defaults(self) -> None:
        del runtime.os.environ["K8S_NAMESPACE"]
        del runtime.os.environ["DEPLOYMENT_ENVIRONMENT"]
        runtime.os.environ["AZURE_ENV_NAME"] = "test"
        code, output = self.invoke("--check-only")
        self.assertEqual(code, 0)
        self.assertIn("no webhook retrieved or resources changed", output)
        self.commands.assert_not_called()

    def test_optional_azd_source_is_captured_and_cli_overrides_process_then_azd(self) -> None:
        self.commands.side_effect = None
        self.commands.return_value = self.response(json.dumps({
            **valid_configuration(), "APPROVAL_TIMEOUT_HOURS": 2,
            "APPROVAL_CALLBACK_AUDIENCE": SENTINEL,
            "LOGIC_APP_APPROVAL_WEBHOOK": SENTINEL,
        }))
        runtime.os.environ["APPROVAL_TIMEOUT_HOURS"] = "invalid-process-value"
        code, output = self.invoke("--check-only", "--from-azd", "--timeout-hours=24")
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, output)
        self.commands.assert_called_once()
        self.assertEqual(self.commands.call_args.args[0], ["azd", "env", "get-values", "--output", "json"])
        self.assertEqual(self.commands.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(self.commands.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_azd_alone_supplies_non_secret_configuration_in_memory(self) -> None:
        values = valid_configuration()
        values.pop("K8S_NAMESPACE")
        self.commands.side_effect = None
        self.commands.return_value = self.response(json.dumps({**values, "APPROVAL_TIMEOUT_HOURS": 2}))
        with patch.dict(runtime.os.environ, {}, clear=True):
            code, output = self.invoke("--check-only", "--from-azd")
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, output)
        self.commands.assert_called_once()

    def test_explicit_empty_does_not_fall_back_to_azd_or_sponsor(self) -> None:
        self.commands.side_effect = None
        self.commands.return_value = self.response(json.dumps({**valid_configuration(), "AGENT_SPONSOR_PRINCIPAL_ID": APPROVER}))
        code, output = self.invoke("--apply", "--from-azd", "--approver-ids=")
        self.assertEqual(code, 2)
        self.assertIn("APPROVAL_APPROVER_IDS", output)
        self.commands.assert_called_once()  # azd read only, no Azure trigger or kubectl

    def test_grouped_azd_output_supports_object_or_encoded_json(self) -> None:
        values = valid_configuration()
        grouped = {name: value for name, value in values.items() if name.startswith("APPROVAL_") or name == "COSMOSDB_APPROVALS_CONTAINER"}
        grouped["APPROVAL_TIMEOUT_HOURS"] = 2
        grouped["LOGIC_APP_APPROVAL_WEBHOOK"] = SENTINEL  # Never selected.
        rest = {name: value for name, value in values.items() if name not in grouped}
        for block in (grouped, json.dumps(grouped)):
            with self.subTest(encoded=isinstance(block, str)), patch.dict(runtime.os.environ, {}, clear=True):
                self.commands.reset_mock()
                self.commands.side_effect = None
                self.commands.return_value = self.response(json.dumps({**rest, "APPROVAL_CALLBACK_PRINCIPAL_ID": "stale", "APPROVAL_RUNTIME_CONFIG": block}))
                code, output = self.invoke("--check-only", "--from-azd")
                self.assertEqual(code, 0, output)
                self.assertNotIn(SENTINEL, output)
                self.commands.assert_called_once()

    def test_process_grouped_output_overrides_stale_flat_but_cli_wins(self) -> None:
        runtime.os.environ["APPROVAL_CALLBACK_PRINCIPAL_ID"] = "stale"
        runtime.os.environ["APPROVAL_RUNTIME_CONFIG"] = json.dumps({
            "APPROVAL_CALLBACK_PRINCIPAL_ID": PRINCIPAL, "APPROVAL_TIMEOUT_HOURS": 2,
        })
        code, output = self.invoke("--check-only")
        self.assertEqual(code, 0, output)
        code, output = self.invoke("--check-only", "--callback-principal-id=")
        self.assertEqual(code, 2)
        self.assertIn("APPROVAL_CALLBACK_PRINCIPAL_ID", output)
        self.commands.assert_not_called()

    def test_invalid_grouped_configuration_fails_before_resource_commands(self) -> None:
        for value in (SENTINEL, "[]", "null", "", "42"):
            with self.subTest(value=value), patch.dict(runtime.os.environ, {"APPROVAL_RUNTIME_CONFIG": value}):
                code, output = self.invoke("--apply")
                self.assertEqual(code, 2)
                self.assertNotIn(SENTINEL, output)
                self.commands.assert_not_called()

    def test_apply_transports_secret_only_in_stdin_and_never_echoes_command_output(self) -> None:
        runtime.os.environ["LOGIC_APP_APPROVAL_WEBHOOK"] = SENTINEL
        runtime.os.environ["APPROVAL_LOGIC_APP_TRIGGER_URL"] = SENTINEL
        runtime.os.environ["AGENT_IDENTITY_ENABLED"] = "false"
        self.commands.side_effect = [
            self.response(json.dumps({"value": WEBHOOK})),
            self.response(SENTINEL), self.response(SENTINEL),
        ]
        code, output = self.invoke("--apply")
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, output)
        calls = self.commands.call_args_list
        self.assertEqual(len(calls), 3)
        target = calls[0].args[0]
        self.assertEqual(target[:4], ["az", "rest", "--method", "POST"])
        self.assertEqual(target[target.index("--url") + 1], (
            f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-test"
            "/providers/Microsoft.Logic/workflows/logic-approval-test/triggers/"
            "When_an_HTTP_request_is_received/listCallbackUrl?api-version=2019-05-01"
        ))
        self.assertEqual(calls[0].kwargs["input"], "")
        secret = json.loads(calls[1].kwargs["input"])
        config_map = json.loads(calls[2].kwargs["input"])
        self.assertEqual(secret, {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "mcp-approval-secrets", "namespace": "mcp-agents"},
            "stringData": {"LOGIC_APP_APPROVAL_WEBHOOK": WEBHOOK},
        })
        self.assertEqual((config_map["apiVersion"], config_map["kind"]), ("v1", "ConfigMap"))
        self.assertEqual(config_map["metadata"], {"name": "mcp-approval-config", "namespace": "mcp-agents"})
        self.assertEqual(config_map["data"], {name: valid_configuration()[name] for name in runtime.RUNTIME_KEYS})
        self.assertNotIn(SENTINEL, calls[2].kwargs["input"])
        for call in calls:
            self.assertNotIn(SENTINEL, json.dumps(call.args))
            self.assertNotIn(SENTINEL, json.dumps(call.kwargs["env"]))
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        for call in calls[1:]:
            self.assertEqual(call.args[0][:2], ["kubectl", "apply"])
            self.assertEqual(call.args[0][-2:], ["-f", "-"])
            self.assertIn("--server-side", call.args[0])
            self.assertNotIn("--force-conflicts", call.args[0])
        self.assertEqual(runtime.os.environ["LOGIC_APP_APPROVAL_WEBHOOK"], SENTINEL)

    def test_bad_azure_responses_prevent_all_kubernetes_writes_and_hide_stderr(self) -> None:
        for response in (
            self.response(SENTINEL, code=1), self.response(SENTINEL),
            self.response(json.dumps({"value": SENTINEL})), self.response("{}"),
            self.response(json.dumps({"value": WEBHOOK.replace("https:", "http:")})),
            self.response(json.dumps({"value": WEBHOOK.split("?", 1)[0]})),
        ):
            with self.subTest(response=response):
                self.commands.reset_mock()
                self.commands.side_effect = [response]
                code, output = self.invoke("--apply")
                self.assertEqual(code, 2)
                self.assertNotIn(SENTINEL, output)
                self.commands.assert_called_once()

    def test_subprocess_exceptions_and_kubectl_failures_do_not_echo_payloads(self) -> None:
        for error in (
            OSError(SENTINEL),
            subprocess.CalledProcessError(1, [SENTINEL], output=WEBHOOK, stderr=WEBHOOK),
            subprocess.TimeoutExpired([SENTINEL], 120, output=WEBHOOK, stderr=WEBHOOK),
        ):
            with self.subTest(error=type(error).__name__):
                self.commands.reset_mock()
                self.commands.side_effect = error
                code, output = self.invoke("--apply")
                self.assertEqual(code, 2)
                self.assertNotIn(SENTINEL, output)
                self.assertNotIn("Traceback", output)
                self.commands.assert_called_once()
        self.commands.reset_mock()
        self.commands.side_effect = [self.response(json.dumps({"value": WEBHOOK})), self.response(WEBHOOK, code=1)]
        code, output = self.invoke("--apply")
        self.assertEqual(code, 2)
        self.assertNotIn(SENTINEL, output)
        self.assertEqual(self.commands.call_count, 2)

    def test_apply_is_explicit_and_argument_errors_never_repeat_input(self) -> None:
        for args in ([], ["--appl"], ["--apply", "--check-only"], ["--apply", "--webhook=" + SENTINEL]):
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(SystemExit) as failure:
                runtime.main(args)
            self.assertEqual(failure.exception.code, 2)
            self.assertNotIn(SENTINEL, output.getvalue())
            self.commands.assert_not_called()

    def test_missing_cli_is_an_explicit_requirement_not_an_install_or_login(self) -> None:
        with patch.object(runtime.shutil, "which", return_value=None):
            code, output = self.invoke("--apply")
        self.assertEqual(code, 2)
        self.assertIn("Required CLI unavailable: az", output)
        self.commands.assert_not_called()


if __name__ == "__main__":
    unittest.main()