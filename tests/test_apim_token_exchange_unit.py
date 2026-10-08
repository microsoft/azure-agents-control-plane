"""Offline source contracts, not APIM expression execution or live federation tests."""

import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OAUTH = ROOT / "infra/app/apim-oauth"


class ManagedIdentityTokenExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_text = (OAUTH / "managed-identity-token-exchange.policy.xml").read_text(encoding="utf-8")
        cls.policy = ET.fromstring(cls.policy_text)
        cls.module = (OAUTH / "oauth.bicep").read_text(encoding="utf-8")

    def test_fragment_is_opt_in_and_uses_deployed_named_value_display_names(self):
        self.assertEqual(self.policy.tag, "fragment")
        for forbidden in ("base", "inbound", "backend", "outbound", "on-error", "include-fragment"):
            self.assertIsNone(self.policy.find(f".//{forbidden}"))
        fragment = self.module.split("resource managedIdentityTokenExchangeFragment ", 1)[1].split("\nresource ", 1)[0]
        self.assertIn("Microsoft.ApiManagement/service/policyFragments@2024-05-01", fragment)
        self.assertIn("name: 'managed-identity-token-exchange'", fragment)
        self.assertIn("loadTextContent('managed-identity-token-exchange.policy.xml')", fragment)
        self.assertIn("format: 'rawxml'", fragment)
        for name in ("EntraIDTenantId", "EntraIDClientId", "EntraIdFicClientId"):
            self.assertIn(f"{name}NamedValue", fragment)
        named_values = set(re.findall(r"displayName: '([^']+)'", self.module))
        self.assertEqual(set(re.findall(r"\{\{([^}]+)\}\}", self.policy_text)), {
            "EntraIDTenantId", "EntraIDClientId", "EntraIdFicClientId",
        })
        self.assertTrue(set(re.findall(r"\{\{([^}]+)\}\}", self.policy_text)) <= named_values)
        self.assertIn("loadTextContent('oauth-callback.policy.simple.xml')", self.module)
        self.assertIn("loadTextContent('token.policy.simple.xml')", self.module)

    def test_scope_guard_precedes_identity_and_uses_only_policy_configuration(self):
        guard = self.policy[0].find("when")
        self.assertIn('GetValueOrDefault<string>("tokenExchangeScope", "")', guard.attrib["condition"])
        self.assertIn('scope.EndsWith("/.default")', guard.attrib["condition"])
        self.assertIn("scope.Any(char.IsWhiteSpace)", guard.attrib["condition"])
        self.assertEqual(guard.find("return-response/set-status").attrib["code"], "500")
        self.assertNotIn("context.Request", self.policy_text)

    def test_assertion_uses_attached_user_assigned_identity_not_app_client_id(self):
        identity = self.policy.find("authentication-managed-identity")
        self.assertEqual(identity.attrib, {
            "resource": "api://AzureADTokenExchange",
            "client-id": "{{EntraIdFicClientId}}",
            "output-token-variable-name": "tokenExchangeAssertion",
            "ignore-error": "false",
        })
        core_module = (ROOT / "infra/core/apim/apim.bicep").read_text(encoding="utf-8")
        self.assertIn("'${entraAppUserAssignedIdentity.id}': {}", core_module)
        self.assertIn("output entraAppUserAssignedIdentityClientId string = entraAppUserAssignedIdentity.properties.clientId", core_module)
        self.assertIn("value: entraAppUserAssignedIdentityClientId", self.module)

    def test_exchange_is_an_isolated_form_encoded_client_credentials_request(self):
        request = self.policy.find("send-request")
        self.assertEqual(request.attrib, {
            "mode": "new", "response-variable-name": "tokenExchangeResponse",
            "timeout": "20", "ignore-error": "true",
        })
        self.assertEqual(request.findtext("set-url"), "https://login.microsoftonline.com/{{EntraIDTenantId}}/oauth2/v2.0/token")
        self.assertEqual(request.findtext("set-method"), "POST")
        self.assertEqual(request.findtext("set-header[@name='Content-Type']/value"), "application/x-www-form-urlencoded")
        self.assertEqual(len(request.findall("set-header")), 1)
        body = request.findtext("set-body")
        for value in ('"{{EntraIDClientId}}"', '(string)context.Variables["tokenExchangeScope"]',
                      '"urn:ietf:params:oauth:client-assertion-type:jwt-bearer"',
                      '(string)context.Variables["tokenExchangeAssertion"]'):
            self.assertIn(f"System.Net.WebUtility.UrlEncode({value})", body)
        self.assertIn("&grant_type=client_credentials", body)
        for forbidden in ("client_secret", "authorization_code", "requested_token_use", "subject_token"):
            self.assertNotIn(forbidden, body)

    def test_transport_and_malformed_token_fail_closed_before_forwarding(self):
        self.assertEqual([element.tag for element in self.policy], [
            "choose", "authentication-managed-identity", "send-request", "choose",
            "set-variable", "choose", "set-header",
        ])
        transport = self.policy[3].find("when")
        for check in ('ContainsKey("tokenExchangeResponse")', '== null', '.StatusCode != 200'):
            self.assertIn(check, transport.attrib["condition"])
        parsing = self.policy[4].attrib["value"]
        for check in ('Body.As<JObject>()', '["access_token"]?.Type != JTokenType.String',
                      '"Bearer", StringComparison.OrdinalIgnoreCase', 'catch', 'return "";'):
            self.assertIn(check, parsing)
        token_guard = self.policy[5].find("when")
        self.assertIn("string.IsNullOrWhiteSpace", token_guard.attrib["condition"])
        for guard in (transport, token_guard):
            response = guard.find("return-response")
            self.assertEqual(response.find("set-status").attrib["code"], "502")
            self.assertEqual(json.loads(response.findtext("set-body")), {"error": "token_exchange_failed"})

    def test_only_final_access_token_is_forwarded_and_no_tokens_are_returned_or_logged(self):
        header = self.policy[-1]
        self.assertEqual(header.attrib, {"name": "Authorization", "exists-action": "override"})
        self.assertEqual(header.findtext("value"), '@("Bearer " + (string)context.Variables["tokenExchangeAccessToken"])')
        for response in self.policy.findall(".//return-response"):
            self.assertEqual(set(json.loads(response.findtext("set-body"))), {"error"})
        for forbidden in ("trace", "log-to-eventhub", "cache-store-value", "set-backend-service", "rewrite-uri"):
            self.assertIsNone(self.policy.find(f".//{forbidden}"))

    def test_documented_example_matches_the_fragment_contract(self):
        guide = (ROOT / "docs/APIM_MANAGED_IDENTITY_TOKEN_EXCHANGE.md").read_text(encoding="utf-8")
        example = re.search(r"```xml\n(.*?)\n```", guide, re.S).group(1)
        inbound = ET.fromstring(f"<inbound>{example}</inbound>")
        self.assertEqual(inbound.find("include-fragment").attrib["fragment-id"], "managed-identity-token-exchange")
        self.assertEqual(inbound.find("set-variable").attrib, {
            "name": "tokenExchangeScope", "value": "https://graph.microsoft.com/.default",
        })
        self.assertEqual(inbound.find("set-backend-service").attrib["base-url"], "https://graph.microsoft.com/v1.0")
        self.assertEqual(inbound.find("rewrite-uri").attrib, {
            "template": "/organization", "copy-unmatched-params": "false",
        })
        credential = json.loads(re.search(r"```json\n(.*?)\n```", guide, re.S).group(1))
        self.assertEqual(credential["subject"], "UAMI_PRINCIPAL_ID")
        self.assertEqual(credential["issuer"], "https://login.microsoftonline.com/TENANT_ID/v2.0")
        self.assertEqual(credential["audiences"], [self.policy.find("authentication-managed-identity").attrib["resource"]])


if __name__ == "__main__":
    unittest.main()