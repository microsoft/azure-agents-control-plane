// Scoped, incremental deployment against existing lab services.
// Stage with enableRouting=false; authorize the Teams connection, then wire the
// private Python backend and callback audience. Never redeploy the whole lab.
targetScope = 'resourceGroup'

param logicAppName string
param cosmosDbAccountName string
param cosmosDbDatabaseName string = 'mcpdb'
param apimServiceName string
param location string = resourceGroup().location
param teamsGroupId string
param teamsChannelId string
param approverIds string[]
param approverTenantId string = subscription().tenantId
@minValue(1)
@maxValue(24)
param approvalTimeoutHours int = 2
param enableRouting bool = false
param callbackAudience string = ''
param backendUrl string = 'http://10.0.4.4'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

var callbackUrl = '${apim.properties.gatewayUrl}/agent-approvals/callback'

module transport './app/agents-approval-logicapp.bicep' = {
  name: 'approval-transport'
  params: {
    logicAppName: logicAppName
    location: location
    cosmosDbAccountName: cosmosDbAccountName
    cosmosDbDatabaseName: cosmosDbDatabaseName
    teamsGroupId: teamsGroupId
    teamsChannelId: teamsChannelId
    approverIds: approverIds
    approverTenantId: approverTenantId
    approvalTimeoutHours: approvalTimeoutHours
    callbackUrl: enableRouting ? callbackUrl : ''
    callbackAudience: enableRouting ? callbackAudience : ''
  }
}

module callback './app/apim-approval-callback.bicep' = if (enableRouting) {
  name: 'approval-callback'
  params: {
    apimServiceName: apimServiceName
    backendUrl: backendUrl
    tenantId: subscription().tenantId
    callbackAudience: callbackAudience
    logicAppPrincipalId: transport.outputs.logicAppPrincipalId
  }
}

// Only non-secret outputs. The signed trigger is retrieved by the runtime
// configuration helper and passed directly to Kubernetes Secret stdin.
output APPROVAL_LOGIC_APP_NAME string = transport.outputs.logicAppName
output APPROVAL_LOGIC_APP_RESOURCE_ID string = transport.outputs.logicAppId
output APPROVAL_TEAMS_CONNECTION_NAME string = transport.outputs.teamsConnectionName
output APPROVAL_CALLBACK_PRINCIPAL_ID string = transport.outputs.logicAppPrincipalId
output APPROVAL_CALLBACK_URL string = callbackUrl
output APPROVAL_CALLBACK_AUDIENCE string = callbackAudience
output APPROVAL_APPROVER_TENANT_ID string = toLower(approverTenantId)
output APPROVAL_APPROVER_IDS string = join(approverIds, ',')
output COSMOSDB_APPROVALS_CONTAINER string = transport.outputs.approvalsContainerName