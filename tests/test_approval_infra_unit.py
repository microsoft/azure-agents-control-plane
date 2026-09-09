"""Offline source-contract tests for the approval transport infrastructure.

Only the assigned Bicep, JSON and XML sources are read. No Azure SDK, credentials,
network, subprocess, deployment, workflow invocation or application import is
used. These checks are not a substitute for Bicep/ARM validation, regional Teams
connector metadata verification, or the Python engine's authorization/idempotency
tests. The parent deployment owns integration and execution.
"""

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any, ClassVar
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
LOGIC_MODULE = ROOT / "infra/app/agents-approval-logicapp.bicep"
WORKFLOW = ROOT / "agent365/workflows/agent_approval_logic_app.json"
APIM_MODULE = ROOT / "infra/app/apim-approval-callback.bicep"
APIM_POLICY = ROOT / "infra/app/apim-approval-callback.policy.xml"
GUID = "11111111-2222-3333-4444-555555555555"
CALLBACK_FIELDS = {
    "approval_id", "environment", "request_hash", "decision", "approved_by",
    "approver_tenant_id", "comment", "workflow_run_id", "timestamp",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently discarding an action."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _action_groups(actions: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield sibling action maps, without treating Adaptive Card actions as WDL."""
    yield actions
    for action in actions.values():
        children = [
            action.get("actions"),
            action.get("else", {}).get("actions"),
            action.get("default", {}).get("actions"),
        ]
        children.extend(case.get("actions") for case in action.get("cases", {}).values())
        for child in children:
            if isinstance(child, dict):
                yield from _action_groups(child)


def _strings(value: Any) -> Iterator[str]:
    """Walk JSON values to inspect expressions without evaluating the workflow."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


class ApprovalInfrastructureTests(unittest.TestCase):
    """Protect the transport boundary and the parent module wiring contract."""

    logic_module: ClassVar[str]
    apim_module: ClassVar[str]
    policy_text: ClassVar[str]
    policy: ClassVar[ET.Element]
    workflow: ClassVar[dict[str, Any]]
    groups: ClassVar[list[dict[str, Any]]]
    actions: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        """Load only local source artifacts, independently of the current cwd."""
        cls.logic_module = LOGIC_MODULE.read_text(encoding="utf-8")
        cls.apim_module = APIM_MODULE.read_text(encoding="utf-8")
        cls.policy_text = APIM_POLICY.read_text(encoding="utf-8")
        cls.policy = ET.fromstring(cls.policy_text)
        cls.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        cls.groups = list(_action_groups(cls.workflow["actions"]))
        cls.actions = {name: action for group in cls.groups for name, action in group.items()}

    def test_module_parameter_and_output_contracts(self) -> None:
        self.assertEqual(set(re.findall(r"(?m)^param (\w+) ", self.logic_module)), {
            "logicAppName", "location", "tags", "cosmosDbAccountName",
            "cosmosDbDatabaseName", "cosmosDbContainerName", "teamsChannelId",
            "teamsGroupId", "approvalTimeoutHours", "callbackUrl", "callbackAudience", "approverIds", "approverTenantId",
        })
        self.assertEqual(set(re.findall(r"(?m)^output (\w+) ", self.logic_module)), {
            "logicAppTriggerUrl", "logicAppId", "logicAppName", "logicAppPrincipalId",
            "teamsConnectionName", "approvalsContainerName",
        })
        self.assertEqual(set(re.findall(r"(?m)^param (\w+) ", self.apim_module)), {
            "apimServiceName", "backendUrl", "tenantId", "callbackAudience", "logicAppPrincipalId",
        })
        self.assertEqual(re.findall(r"(?m)^output (\w+) ", self.apim_module), ["callbackUrl"])

    def test_bicep_loads_one_canonical_definition_and_binds_all_parameters(self) -> None:
        paths = re.findall(r"definition:\s*loadJsonContent\('([^']+)'\)", self.logic_module)
        self.assertEqual(len(paths), 1)
        self.assertEqual((LOGIC_MODULE.parent / paths[0]).resolve(), WORKFLOW.resolve())
        self.assertNotRegex(self.logic_module, r"definition:\s*\{")
        supplied = self.logic_module.split("    parameters: {", 1)[1].split("\n    }", 1)[0]
        bound = {name.strip("'") for name in re.findall(r"(?m)^      ('\$connections'|\w+): \{", supplied)}
        self.assertEqual(bound, set(self.workflow["parameters"]))
        self.assertIn("value: toLower(trim(approverTenantId))", supplied)
        self.assertIn("value: map(approverIds, id => toLower(trim(id)))", supplied)
        self.assertEqual(self.workflow["outputs"], {})

    def test_identity_is_system_assigned_and_trigger_output_is_secure(self) -> None:
        self.assertIn("Microsoft.Logic/workflows@2019-05-01", self.logic_module)
        self.assertRegex(self.logic_module, r"identity:\s*\{\s*type: 'SystemAssigned'\s*\}")
        self.assertNotIn("userAssignedIdentit", self.logic_module)
        self.assertIn("output logicAppPrincipalId string = logicApp.identity.principalId", self.logic_module)
        self.assertRegex(self.logic_module, r"@secure\(\)\s*output logicAppTriggerUrl string = listCallbackUrl\(")
        self.assertIn("/triggers/When_an_HTTP_request_is_received", self.logic_module)
        self.assertNotIn("#disable-next-line outputs-should-not-contain-secrets", self.logic_module)

    def test_staged_routing_disables_the_workflow_and_bounds_wait(self) -> None:
        for parameter in ("teamsChannelId", "teamsGroupId", "callbackUrl", "callbackAudience"):
            self.assertIn(f"param {parameter} string = ''", self.logic_module)
        self.assertIn("param approverIds string[] = []", self.logic_module)
        self.assertIn("state: workflowConfigured ? 'Enabled' : 'Disabled'", self.logic_module)
        configured = self.logic_module.split("var workflowConfigured =", 1)[1].split("\n\n", 1)[0]
        for parameter in ("teamsChannelId", "teamsGroupId", "approverIds", "callbackUrl", "callbackAudience"):
            self.assertIn(parameter, configured)
        self.assertIn("startsWith(callbackUrl, 'https://')", configured)
        self.assertIn("endsWith(callbackUrl, '/agent-approvals/callback')", configured)
        self.assertRegex(self.logic_module, r"@minValue\(1\)\s*@maxValue\(24\)\s*param approvalTimeoutHours int = 2")
        guard = self.actions["Validate_Deployment_Configuration"]
        self.assertEqual(guard["type"], "ParseJson")
        self.assertEqual(guard["inputs"]["schema"]["properties"]["approverIds"]["minItems"], 1)
        self.assertEqual(self.actions["Stop_On_Invalid_Configuration"]["inputs"]["runStatus"], "Failed")

    def test_cosmos_container_is_durable_and_workflow_has_no_data_privileges(self) -> None:
        self.assertRegex(self.logic_module, r"resource cosmosDatabase '[^']+/sqlDatabases@[^']+' existing")
        self.assertIn("parent: cosmosDatabase", self.logic_module)
        self.assertIn("name: cosmosDbContainerName", self.logic_module)
        self.assertIn("id: cosmosDbContainerName", self.logic_module)
        self.assertIn("paths: ['/environment']", self.logic_module)
        self.assertIn("defaultTtl: -1", self.logic_module)
        sources = self.logic_module + json.dumps(self.workflow)
        for forbidden in ("listKeys", "primaryMasterKey", "accessKey", "sqlRoleAssignments", "['documentdb']"):
            self.assertNotIn(forbidden, sources)
        self.assertNotIn("cosmosDbEndpoint", self.workflow["parameters"])
        self.assertEqual({a["type"] for a in self.actions.values()}, {
            "Response", "ParseJson", "Scope", "Compose", "ApiConnectionWebhook", "Http", "Terminate",
        })

    def test_teams_connection_is_oauth_not_managed_identity_or_reset_tokens(self) -> None:
        connections = re.findall(r"resource (\w+) 'Microsoft.Web/connections@", self.logic_module)
        self.assertEqual(connections, ["teamsConnection"])
        connection = self.logic_module.split("resource teamsConnection", 1)[1].split("resource logicApp", 1)[0]
        self.assertIn("kind: 'V1'", connection)
        self.assertIn("location, 'teams')", connection)
        self.assertNotIn("parameterValues", connection)
        self.assertNotIn("parameterValueSet", connection)
        self.assertNotIn("ManagedServiceIdentity", connection)

    def test_deployment_resource_names_are_repeatable(self) -> None:
        for source in (self.logic_module, self.apim_module):
            self.assertNotRegex(source, r"\b(newGuid|utcNow)\s*\(")
            self.assertNotIn("Microsoft.Resources/deploymentScripts", source)
        self.assertIn("name: '${logicAppName}-teams'", self.logic_module)
        self.assertIn("name: 'agent-approvals'", self.apim_module)
        self.assertIn("name: 'callback'", self.apim_module)
        self.assertIn("name: 'policy'", self.apim_module)

    def test_trigger_schema_is_a_bound_request_not_a_decision_or_route(self) -> None:
        self.assertEqual(set(self.workflow["triggers"]), {"When_an_HTTP_request_is_received"})
        trigger = self.workflow["triggers"]["When_an_HTTP_request_is_received"]
        self.assertEqual((trigger["type"], trigger["kind"], trigger["inputs"]["method"]), ("Request", "Http", "POST"))
        self.assertEqual(trigger["operationOptions"], "EnableSchemaValidation")
        schema = trigger["inputs"]["schema"]
        expected = {
            "approval_id", "request_hash", "task", "environment", "requested_by", "cluster",
            "namespace", "image_tags", "commit_sha", "request_timestamp", "expires_at", "approvers",
            "pipeline_url", "rollback_url",
        }
        self.assertEqual(set(schema["required"]), expected)
        self.assertEqual(set(schema["properties"]), expected)
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["image_tags"]["items"]["type"], "string")
        self.assertEqual(schema["properties"]["approvers"]["minItems"], 1)
        for key in ("request_timestamp", "expires_at"):
            self.assertEqual(schema["properties"][key]["format"], "date-time")
        # Azure rejects regex keywords in BOTH validated Request triggers and
        # ParseJson actions. Python owns full GUID/hash format validation.
        self.assertNotIn('"pattern"', json.dumps(schema))
        self.assertNotIn('"patternProperties"', json.dumps(schema))
        self.assertEqual(schema["properties"]["request_hash"]["minLength"], 64)
        self.assertEqual(schema["properties"]["request_hash"]["maxLength"], 64)
        identifiers = self.actions["Validate_Request_Identifiers"]
        self.assertEqual(identifiers["type"], "ParseJson")
        self.assertEqual(identifiers["runAfter"], {})
        self.assertEqual(self.actions["Request_Is_Expired"]["runAfter"], {"Validate_Request_Identifiers": ["Succeeded"]})
        properties = identifiers["inputs"]["schema"]["properties"]
        self.assertEqual(set(identifiers["inputs"]["content"]), {"approval_id", "request_hash", "approvers"})
        for prop, size in ((properties["approval_id"], 36), (properties["approvers"]["items"], 36), (properties["request_hash"], 64)):
            self.assertEqual((prop["type"], prop["minLength"], prop["maxLength"]), ("string", size, size))

    def test_supported_schemas_and_exact_https_routing_guard(self) -> None:
        schemas = [self.workflow["triggers"]["When_an_HTTP_request_is_received"]["inputs"]["schema"]]
        schemas.extend(action["inputs"]["schema"] for action in self.actions.values() if action["type"] == "ParseJson")
        for schema in schemas:
            self.assertNotIn('"pattern"', json.dumps(schema))
            self.assertNotIn('"patternProperties"', json.dumps(schema))
        guard = self.actions["Validate_Deployment_Configuration"]["inputs"]
        properties = guard["schema"]["properties"]
        self.assertEqual(guard["content"]["callbackUrlIsSafe"],
            "@and(equals(uriScheme(parameters('callbackUrl')), 'https'), not(empty(uriHost(parameters('callbackUrl')))), or(equals(parameters('callbackUrl'), concat('https://', uriHost(parameters('callbackUrl')), '/agent-approvals/callback')), equals(parameters('callbackUrl'), concat('https://', uriHost(parameters('callbackUrl')), ':443/agent-approvals/callback'))))")
        # Whole-URL equality against a reconstructed HTTPS host/path prohibits
        # credentials, queries, fragments, alternate paths and non-443 ports.
        for field in ("callbackUrlIsSafe", "channelIsSupported", "audienceIsResourceUri"):
            self.assertEqual(properties[field], {"type": "boolean", "enum": [True]})
            self.assertIn(field, guard["schema"]["required"])
        self.assertIn("startsWith(parameters('teamsChannelId'), '19:')", guard["content"]["channelIsSupported"])
        self.assertEqual(guard["content"]["audienceIsResourceUri"], "@startsWith(parameters('callbackAudience'), 'api://')")
        for field in ("teamsGroupId", "approverTenantId"):
            self.assertEqual(properties[field], {"type": "string", "minLength": 36, "maxLength": 36})
        self.assertEqual(properties["callbackAudience"], {"type": "string", "minLength": 42, "maxLength": 42})

    def test_workflow_graph_has_unique_names_valid_sibling_edges_and_no_cycles(self) -> None:
        self.assertEqual(len(self.actions), sum(len(group) for group in self.groups))
        for group in self.groups:
            seen: set[str] = set()
            active: set[str] = set()

            def visit(name: str) -> None:
                """Check runAfter edges within their containing scope."""
                self.assertNotIn(name, active, f"Cycle at {name}")
                if name in seen:
                    return
                active.add(name)
                for predecessor, statuses in group[name]["runAfter"].items():
                    self.assertIn(predecessor, group, f"Invalid cross-scope edge: {name}")
                    self.assertTrue(statuses)
                    self.assertLessEqual(set(statuses), {"Succeeded", "Failed", "TimedOut", "Skipped"})
                    visit(predecessor)
                active.remove(name)
                seen.add(name)

            for name in group:
                visit(name)

    def test_expression_references_and_delimiters_are_well_formed(self) -> None:
        for expression in _strings(self.workflow):
            if not expression.startswith("@"):
                continue
            with self.subTest(expression=expression):
                for reference in re.findall(r"(?:body|outputs|actions)\('([^']+)'\)", expression):
                    self.assertIn(reference, self.actions)
                for parameter in re.findall(r"parameters\('([^']+)'\)", expression):
                    self.assertIn(parameter, self.workflow["parameters"])
                # WDL escapes a quote inside a string with two single quotes.
                unquoted = re.sub(r"'(?:[^']|'')*'", "", expression)
                self.assertNotIn("'", unquoted)
                stack: list[str] = []
                for char in unquoted:
                    if char in "([":
                        stack.append(char)
                    elif char in ")]":
                        self.assertTrue(stack, "Unmatched closing delimiter")
                        self.assertEqual(stack.pop(), {")": "(", "]": "["}[char])
                self.assertEqual(stack, [])

    def test_single_immediate_202_dominates_all_outbound_calls(self) -> None:
        root = self.workflow["actions"]
        self.assertEqual([name for name, action in root.items() if not action["runAfter"]], ["Acknowledge_Request"])
        responses = [name for name, action in self.actions.items() if action["type"] == "Response"]
        self.assertEqual(responses, ["Acknowledge_Request"])
        response = root["Acknowledge_Request"]
        self.assertEqual(response["inputs"]["statusCode"], 202)
        self.assertEqual(response["inputs"]["body"]["status"], "accepted")
        self.assertEqual(response["inputs"]["body"]["workflow_run_id"], "@workflow().run.name")
        self.assertEqual(root["Validate_Deployment_Configuration"]["runAfter"], {"Acknowledge_Request": ["Succeeded"]})
        self.assertEqual(root["Collect_Decision"]["runAfter"], {"Validate_Deployment_Configuration": ["Succeeded"]})
        self.assertNotIn("Asynchronous", response.get("operationOptions", ""))

    def test_every_data_bearing_action_hides_run_history(self) -> None:
        trigger = self.workflow["triggers"]["When_an_HTTP_request_is_received"]
        self.assertEqual(set(trigger["runtimeConfiguration"]["secureData"]["properties"]), {"inputs", "outputs"})
        for name, action in self.actions.items():
            if action["type"] in {"Scope", "Terminate"}:
                continue
            with self.subTest(action=name):
                secured = set(action["runtimeConfiguration"]["secureData"]["properties"])
                self.assertIn("inputs", secured)
                # Compose, ParseJson and Response automatically hide their
                # outputs with secure inputs; no unsupported outputs toggle.
                if action["type"] in {"Http", "ApiConnectionWebhook"}:
                    self.assertIn("outputs", secured)
                else:
                    self.assertEqual(secured, {"inputs"})
                self.assertNotIn("trackedProperties", action)

    def test_teams_operation_is_card_webhook_with_deployment_routing(self) -> None:
        self.assertEqual(self.workflow["metadata"]["teamsOperationId"], "PostCardAndWaitForResponse")
        teams = self.actions["Wait_for_Teams_Response"]
        self.assertEqual(teams["type"], "ApiConnectionWebhook")
        self.assertEqual(teams["inputs"]["path"], "/v1.0/teams/conversation/gatherinput/poster/Flow%20bot/location/Channel/$subscriptions")
        self.assertEqual(teams["inputs"]["host"]["connection"]["name"], "@parameters('$connections')['teams']['connectionId']")
        body = teams["inputs"]["body"]
        self.assertEqual(body["notificationUrl"], "@listCallbackUrl()")
        self.assertEqual(body["body"]["recipient"], {
            "groupId": "@parameters('teamsGroupId')", "channelId": "@parameters('teamsChannelId')",
        })
        self.assertEqual(body["body"]["messageBody"], "@string(outputs('Build_Adaptive_Card'))")
        self.assertEqual(set(body), {"notificationUrl", "body"})
        self.assertEqual(set(body["body"]), {"recipient", "messageBody", "updateMessage"})
        self.assertEqual(teams["inputs"]["retryPolicy"], {"type": "none"})
        self.assertNotIn("/v2/approvals/create", json.dumps(self.workflow))
        self.assertEqual(sum("listCallbackUrl()" in text for text in _strings(self.workflow)), 1)

    def test_card_collects_only_decision_and_comment_not_identity(self) -> None:
        card = self.actions["Build_Adaptive_Card"]["inputs"]
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual([action["data"] for action in card["actions"]], [{"decision": "approved"}, {"decision": "rejected"}])
        self.assertTrue(all(action["type"] == "Action.Submit" for action in card["actions"]))
        inputs = [item for item in card["body"] if item["type"].startswith("Input.")]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["id"], "comment")
        self.assertEqual(inputs[0]["maxLength"], 2000)

    def test_identity_comes_only_from_connector_envelope(self) -> None:
        parsed = self.actions["Parse_Teams_Response"]["inputs"]
        for key in ("approved_by", "approver_tenant_id"):
            expression = parsed["content"][key]
            self.assertIn("body('Wait_for_Teams_Response')?['responder']", expression)
            for forbidden in ("['data']", "triggerBody", "displayName", "email", "requested_by"):
                self.assertNotIn(forbidden, expression)
            self.assertEqual(parsed["schema"]["properties"][key], {"type": "string", "minLength": 36, "maxLength": 36})
        self.assertIn("['objectId']", parsed["content"]["approved_by"])
        self.assertIn("['id']", parsed["content"]["approved_by"])
        self.assertIn("['tenantId']", parsed["content"]["approver_tenant_id"])

    def test_normalization_and_exact_allowlists_fail_closed(self) -> None:
        raw = self.actions["Normalize_Submitted_Decision"]["inputs"]
        self.assertIn("@toLower(trim(", raw)
        parsed = self.actions["Parse_Teams_Response"]["inputs"]
        self.assertEqual(parsed["schema"]["properties"]["decision"]["enum"], ["approved", "rejected"])
        self.assertIn("createArray('approve', 'approved')", parsed["content"]["decision"])
        self.assertIn("createArray('reject', 'rejected')", parsed["content"]["decision"])
        self.assertTrue(parsed["content"]["decision"].endswith("'rejected', 'error'))"))
        authorized = self.actions["Responder_Is_Authorized"]["inputs"]
        self.assertTrue(authorized.startswith("@and("))
        self.assertIn("contains(json(toLower(string(parameters('approverIds'))))", authorized)
        self.assertIn("contains(json(toLower(string(triggerBody()?['approvers'])))", authorized)
        self.assertIn("equals(body('Parse_Teams_Response')?['approver_tenant_id'], toLower(parameters('approverTenantId')))", authorized)
        decision = self.actions["Select_Terminal_Decision"]["inputs"]
        self.assertTrue(decision.startswith("@if(outputs('Responder_Is_Authorized'), if("))
        self.assertTrue(decision.endswith("body('Parse_Teams_Response')?['decision']), 'error')"))

    def test_expiry_and_empty_approver_intersection_prevent_posting(self) -> None:
        context = self.actions["Validate_Request_Context"]["inputs"]
        self.assertIn("intersection(", context["content"]["has_eligible_approver"])
        self.assertEqual(context["schema"]["properties"]["unexpired"]["enum"], [True])
        self.assertEqual(context["schema"]["properties"]["has_eligible_approver"]["enum"], [True])
        self.assertEqual(self.actions["Build_Adaptive_Card"]["runAfter"], {"Validate_Request_Context": ["Succeeded"]})
        timeout = self.actions["Wait_for_Teams_Response"]["limit"]["timeout"]
        for expected in ("max(1, min(", "parameters('approvalTimeoutHours')", "triggerBody()?['expires_at']", "ticks(utcNow())"):
            self.assertIn(expected, timeout)
        self.assertIn("lessOrEquals(ticks(triggerBody()?['expires_at']), ticks(utcNow()))", self.actions["Select_Terminal_Decision"]["inputs"])

    def test_callback_payloads_bind_request_and_run_without_untrusted_overrides(self) -> None:
        for name in ("Build_Decision_Callback", "Build_Failure_Callback"):
            body = self.actions[name]["inputs"]
            self.assertEqual(set(body), CALLBACK_FIELDS)
            for key in ("approval_id", "environment", "request_hash"):
                self.assertEqual(body[key], f"@triggerBody()?['{key}']")
            self.assertEqual(body["workflow_run_id"], "@workflow().run.name")
            self.assertEqual(body["timestamp"], "@utcNow()")
            for key in ("decision", "approved_by", "approver_tenant_id", "comment"):
                self.assertNotIn("triggerBody", body[key])
        failure = self.actions["Build_Failure_Callback"]["inputs"]
        self.assertEqual(failure["approved_by"], "system")
        self.assertEqual(failure["approver_tenant_id"], "")
        self.assertIn("'timeout', 'error'", failure["decision"])
        self.assertIn("ActionTimedOut", failure["decision"])
        self.assertNotIn("'approved'", failure["decision"])
        actions_text = json.dumps(self.workflow["actions"])
        for forbidden in ("callback_url", "agent_validation", "X-Agent-Validation", "displayName", "approver_email"):
            self.assertNotIn(forbidden, actions_text)

    def test_callback_authentication_retry_and_idempotency_contract(self) -> None:
        callbacks = [action for action in self.actions.values() if action["type"] == "Http"]
        self.assertEqual(len(callbacks), 2)
        for action in callbacks:
            inputs = action["inputs"]
            self.assertEqual(inputs["method"], "POST")
            self.assertEqual(inputs["uri"], "@parameters('callbackUrl')")
            self.assertEqual(inputs["authentication"], {
                "type": "ManagedServiceIdentity", "audience": "@parameters('callbackAudience')",
            })
            self.assertEqual(inputs["headers"], {"Content-Type": "application/json"})
            self.assertEqual(inputs["retryPolicy"], {
                "type": "exponential", "count": 3, "interval": "PT10S",
                "minimumInterval": "PT10S", "maximumInterval": "PT1M",
            })
            # Freeze timestamp and identity in a Compose before the initial
            # POST; every retry carries the identical request/run correlation.
            self.assertRegex(inputs["body"], r"^@outputs\('Build_(Decision|Failure)_Callback'\)$")
            self.assertEqual(action["operationOptions"], "DisableAsyncPattern")
        self.assertEqual(self.actions["Notify_Decision"]["runAfter"], {"Collect_Decision": ["Succeeded"]})
        self.assertEqual(self.actions["Handle_Timeout_Or_Error"]["runAfter"], {"Collect_Decision": ["Failed", "TimedOut"]})
        # An ambiguous callback delivery failure must NOT emit a competing error
        # decision. Only collection failures reach the failure callback scope.
        self.assertFalse(any(action["type"] == "Http" for action in self.actions["Collect_Decision"]["actions"].values()))
        self.assertNotIn("staticResults", self.workflow)
        self.assertNotIn("staticResult", json.dumps(self.workflow["actions"]))

    def test_apim_exposes_only_separate_https_post_callback_after_policy(self) -> None:
        self.assertIn("path: 'agent-approvals'", self.apim_module)
        self.assertIn("protocols: ['https']", self.apim_module)
        self.assertIn("subscriptionRequired: false", self.apim_module)
        self.assertIn("serviceUrl: backendUrl", self.apim_module)
        self.assertEqual(re.findall(r"method: '([^']+)'", self.apim_module), ["POST"])
        self.assertEqual(re.findall(r"urlTemplate: '([^']+)'", self.apim_module), ["/callback"])
        self.assertIn("dependsOn: [approvalApiPolicy]", self.apim_module)
        self.assertIn("loadTextContent('apim-approval-callback.policy.xml')", self.apim_module)
        self.assertIn("${apimService.properties.gatewayUrl}/agent-approvals/callback", self.apim_module)
        rewrite = self.policy.find("./inbound/rewrite-uri")
        self.assertIsNotNone(rewrite)
        assert rewrite is not None
        self.assertEqual(rewrite.attrib, {"template": "/approvals/callback", "copy-unmatched-params": "false"})

    def test_apim_requires_signed_unexpired_tenant_and_system_principal_token(self) -> None:
        jwt = self.policy.find("./inbound/validate-jwt")
        self.assertIsNotNone(jwt)
        assert jwt is not None
        for key, value in {
            "header-name": "Authorization", "require-scheme": "Bearer", "require-signed-tokens": "true",
            "require-expiration-time": "true", "failed-validation-httpcode": "401",
        }.items():
            self.assertEqual(jwt.get(key), value)
        self.assertLessEqual(int(jwt.attrib["clock-skew"]), 60)
        self.assertEqual([node.text for node in jwt.findall("./audiences/audience")], ["__CALLBACK_AUDIENCE__", "__CALLBACK_CLIENT_ID__"])
        self.assertEqual({node.text for node in jwt.findall("./issuers/issuer")}, {
            "https://sts.windows.net/__TENANT_ID__/",
            "https://login.microsoftonline.com/__TENANT_ID__/v2.0",
        })
        self.assertEqual({node.attrib["url"] for node in jwt.findall("openid-config")}, {
            "https://login.microsoftonline.com/__TENANT_ID__/.well-known/openid-configuration",
            "https://login.microsoftonline.com/__TENANT_ID__/v2.0/.well-known/openid-configuration",
        })
        claims = {node.attrib["name"]: node for node in jwt.findall("./required-claims/claim")}
        self.assertEqual(set(claims), {"tid", "oid"})
        for claim, value in (("tid", "__TENANT_ID__"), ("oid", "__LOGIC_APP_PRINCIPAL_ID__")):
            self.assertEqual(claims[claim].get("match"), "all")
            self.assertEqual([node.text for node in claims[claim].findall("value")], [value])

    def test_apim_preserves_bearer_and_does_not_reuse_mcp_auth(self) -> None:
        inbound = self.policy.find("inbound")
        assert inbound is not None
        names = [node.tag for node in inbound]
        self.assertEqual(names[0], "validate-jwt")
        self.assertLess(names.index("validate-jwt"), names.index("base"))
        original = inbound.find("set-variable[@name='approvalAuthorization']")
        assert original is not None
        self.assertIn('GetValueOrDefault("Authorization", "")', original.attrib["value"])
        preserved = inbound.find("set-header[@name='Authorization']")
        assert preserved is not None
        self.assertEqual(preserved.attrib["exists-action"], "override")
        self.assertEqual(preserved.findtext("value"), '@((string)context.Variables["approvalAuthorization"])')
        self.assertLess(names.index("base"), list(inbound).index(preserved))
        for forbidden in ("EncryptionKey", "EncryptionIV", "Decrypt(", "x-functions-key", "Ocp-Apim-Subscription-Key", "{{"):
            self.assertNotIn(forbidden, self.policy_text)

    def test_apim_limits_size_and_rate_without_cache_redirects_or_retries(self) -> None:
        size = self.policy.find("./inbound/validate-content")
        assert size is not None
        self.assertEqual(size.get("max-size"), "16384")
        self.assertEqual(size.get("size-exceeded-action"), "prevent")
        rate = self.policy.find("./inbound/rate-limit-by-key")
        assert rate is not None
        self.assertEqual(rate.attrib, {
            "calls": "60", "renewal-period": "60", "counter-key": "agent-approvals:__LOGIC_APP_PRINCIPAL_ID__",
        })
        forward = self.policy.find("./backend/forward-request")
        assert forward is not None
        self.assertEqual(forward.attrib, {"timeout": "30", "follow-redirects": "false"})
        unsupported_type = self.policy.find("./inbound/choose/when/return-response/set-status")
        assert unsupported_type is not None
        self.assertEqual(unsupported_type.get("code"), "415")
        for section in ("outbound", "on-error"):
            self.assertEqual(self.policy.findtext(f"./{section}/set-header[@name='Cache-Control']/value"), "no-store")
        for node in self.policy.iter():
            self.assertFalse(node.tag.startswith("cache-"))
            self.assertNotIn(node.tag, {"retry", "send-request", "authentication-managed-identity"})

    def test_policy_substitutions_are_deterministic_xml_safe_and_complete(self) -> None:
        tokens = {
            "__TENANT_ID__": GUID,
            "__CALLBACK_AUDIENCE__": f"api://{GUID}",
            "__CALLBACK_CLIENT_ID__": GUID,
            "__LOGIC_APP_PRINCIPAL_ID__": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
        self.assertEqual(set(re.findall(r"__[A-Z_]+__", self.policy_text)), set(tokens))
        rendered = self.policy_text
        for token, value in tokens.items():
            self.assertIn(f"'{token}', escapeXml(", self.apim_module)
            rendered = rendered.replace(token, value)
        self.assertNotRegex(rendered, r"__[A-Z_]+__")
        parsed = ET.fromstring(rendered)
        self.assertEqual(parsed.findtext("./inbound/validate-jwt/required-claims/claim[@name='oid']/value"), tokens["__LOGIC_APP_PRINCIPAL_ID__"])
        self.assertIn("substring(callbackAudience, 6)", self.apim_module)
        for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&apos;"):
            self.assertIn(entity, self.apim_module)
        # Re-rendering the same source with the same parameters has no mutable
        # named values, timestamps, deployment scripts or external side effects.
        repeated = self.policy_text
        for token, value in tokens.items():
            repeated = repeated.replace(token, value)
        self.assertEqual(rendered, repeated)