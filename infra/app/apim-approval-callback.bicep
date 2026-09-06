// Separate API: never attach this operation to the MCP API and its custom token
// policy. The parent wires private networking, tenant/audience, and the Logic
// App's system-assigned principal, and keeps those same validators in Python.

@description('Existing API Management service name.')
@minLength(1)
param apimServiceName string

@description('Private Python service origin, e.g. http://10.0.4.4, without a path, query, fragment or credentials. Prefer HTTPS when the backend supports it; HTTP is only for the private VNet hop.')
@minLength(1)
@maxLength(2048)
param backendUrl string

@description('Entra tenant GUID for the Logic App system identity and callback application (Azure public cloud).')
@minLength(36)
@maxLength(36)
param tenantId string

@description('Callback application ID URI, exactly api://<blueprint-client-GUID>. Both URI (v1) and GUID (v2) audiences are accepted.')
@minLength(42)
@maxLength(42)
param callbackAudience string

@description('Logic App SYSTEM-ASSIGNED service principal OBJECT ID for the oid allowlist; never a client ID, agent identity or UAMI principal.')
@minLength(36)
@maxLength(36)
param logicAppPrincipalId string

// Escape configuration as XML, not policy source. The placeholders below are
// resolved at deployment; they are not mutable APIM named values or secrets.
func escapeXml(value string) string => replace(replace(replace(replace(replace(value, '&', '&amp;'), '<', '&lt;'), '>', '&gt;'), '"', '&quot;'), '\'', '&apos;')

var policyWithTenant = replace(loadTextContent('apim-approval-callback.policy.xml'), '__TENANT_ID__', escapeXml(toLower(tenantId)))
var policyWithAudience = replace(policyWithTenant, '__CALLBACK_AUDIENCE__', escapeXml(toLower(callbackAudience)))
var policyWithClientId = replace(policyWithAudience, '__CALLBACK_CLIENT_ID__', escapeXml(toLower(substring(callbackAudience, 6))))
var callbackPolicy = replace(policyWithClientId, '__LOGIC_APP_PRINCIPAL_ID__', escapeXml(toLower(logicAppPrincipalId)))

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

resource approvalApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apimService
  name: 'agent-approvals'
  properties: {
    displayName: 'Agent Approval Callback'
    description: 'Authenticated Logic App decision transport to the private Python approval engine.'
    path: 'agent-approvals'
    protocols: ['https']
    subscriptionRequired: false
    serviceUrl: backendUrl
  }
}

resource approvalApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: approvalApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: callbackPolicy
  }
}

resource callbackOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: approvalApi
  name: 'callback'
  properties: {
    displayName: 'Receive Approval Decision'
    description: 'Python authenticates again, validates the stored request and applies an idempotent Cosmos transition.'
    method: 'POST'
    urlTemplate: '/callback'
  }
  // Do not expose the operation before its API authentication policy exists.
  dependsOn: [approvalApiPolicy]
}

@description('Non-secret HTTPS callback URL. Compute this same URL from the gateway for the Logic App input to avoid a circular module dependency.')
output callbackUrl string = '${apimService.properties.gatewayUrl}/agent-approvals/callback'