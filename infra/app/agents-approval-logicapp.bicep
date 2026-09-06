// Consumption approval transport. Python alone validates decisions and persists
// approval/audit records; this workflow has no Cosmos data access.
// Requires Bicep >= 0.35.1 for the secure trigger URL output.

@description('The name of the Consumption Logic App.')
@minLength(1)
@maxLength(80)
param logicAppName string

@description('The location for the workflow and Teams managed API connection.')
param location string = resourceGroup().location

@description('Tags to apply to the workflow and connection.')
param tags object = {}

@description('Existing Cosmos DB account used by the Python approval engine.')
@minLength(1)
param cosmosDbAccountName string

@description('Existing SQL database. The parent MUST depend on database provisioning before deploying this module.')
@minLength(1)
param cosmosDbDatabaseName string = 'mcpdb'

@description('Approval/audit container, partitioned by /environment. Python is its only application writer.')
@minLength(1)
param cosmosDbContainerName string = 'approvals'

@description('Fixed Teams channel ID. Empty routing is allowed for staged provisioning and disables the workflow.')
@maxLength(256)
param teamsChannelId string = ''

@description('Fixed Teams team/group object ID (GUID). Empty routing disables the workflow.')
@maxLength(36)
param teamsGroupId string = ''

@description('Maximum Teams wait, in hours. The request expires_at can shorten this; Python enforces the durable deadline.')
@minValue(1)
@maxValue(24)
param approvalTimeoutHours int = 2

@description('Fixed HTTPS APIM URL ending in /agent-approvals/callback, with no query, fragment or credentials. Empty disables the workflow. Compute from the gateway, not from the callback module output, to avoid a dependency cycle.')
@maxLength(2048)
param callbackUrl string = ''

@description('Entra application ID URI api://<blueprint-client-GUID> for the callback access token. Empty disables the workflow.')
@maxLength(42)
param callbackAudience string = ''

@description('Allowed approver Entra user object IDs (GUIDs), in this subscription tenant. Request approvers may narrow but never widen this list. Empty disables the workflow.')
@maxLength(100)
param approverIds string[] = []

// Stage 1 creates the system identity, connection and container without routing.
// Authorize the Teams OAuth connection as a user, wire APIM with the principal
// output, then provide routing. The workflow also validates configuration before
// any outbound call, including if an operator manually enables an incomplete app.
var workflowConfigured = !empty(trim(teamsChannelId))
  && !empty(trim(teamsGroupId))
  && !empty(approverIds)
  && startsWith(callbackUrl, 'https://')
  && endsWith(callbackUrl, '/agent-approvals/callback')
  && startsWith(callbackAudience, 'api://')
  && length(callbackAudience) == 42

resource cosmosDbAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosDbAccountName
}

// Consumption uses a V1 managed API connection. Teams requires interactive user
// OAuth authorization; the workflow managed identity CANNOT authorize Teams.
// Do not replace the user's authorization with empty tokens on redeployment.
resource teamsConnection 'Microsoft.Web/connections@2016-06-01' = {
  name: '${logicAppName}-teams'
  location: location
  tags: tags
  kind: 'V1'
  properties: {
    displayName: 'Teams Connection for Agent Approvals'
    api: {
      id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'teams')
    }
  }
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'agents-approval-workflow'
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: workflowConfigured ? 'Enabled' : 'Disabled'
    definition: loadJsonContent('../../agent365/workflows/agent_approval_logic_app.json')
    parameters: {
      '$connections': {
        value: {
          teams: {
            connectionId: teamsConnection.id
            connectionName: teamsConnection.name
            id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'teams')
          }
        }
      }
      teamsChannelId: {
        value: trim(teamsChannelId)
      }
      teamsGroupId: {
        value: toLower(trim(teamsGroupId))
      }
      approvalTimeoutHours: {
        value: approvalTimeoutHours
      }
      callbackUrl: {
        value: callbackUrl
      }
      callbackAudience: {
        value: toLower(callbackAudience)
      }
      approverIds: {
        value: map(approverIds, id => toLower(trim(id)))
      }
      approverTenantId: {
        value: toLower(subscription().tenantId)
      }
    }
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' existing = {
  parent: cosmosDbAccount
  name: cosmosDbDatabaseName
}

resource approvalsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDatabase
  name: cosmosDbContainerName
  properties: {
    resource: {
      id: cosmosDbContainerName
      partitionKey: {
        paths: ['/environment']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [
          { path: '/*' }
        ]
        excludedPaths: [
          { path: '/"_etag"/?' }
        ]
      }
      defaultTtl: -1
    }
  }
}

@description('Sensitive SAS trigger URL. Never promote to an ordinary parent/azd output, environment file or manifest. The parent may instead retrieve listCallbackUrl and inject directly into a Kubernetes Secret without logging it.')
@secure()
output logicAppTriggerUrl string = listCallbackUrl('${logicApp.id}/triggers/When_an_HTTP_request_is_received', '2019-05-01').value

@description('The Logic App resource ID.')
output logicAppId string = logicApp.id

@description('The Logic App resource name.')
output logicAppName string = logicApp.name

@description('System-assigned service principal OBJECT ID for the APIM oid allowlist and Python callback validation. Not a client ID or an agent/UAMI principal.')
output logicAppPrincipalId string = logicApp.identity.principalId

@description('Teams OAuth managed API connection name; an authorized user must authorize this connection before use.')
output teamsConnectionName string = teamsConnection.name

@description('Approvals container name for the Python engine.')
output approvalsContainerName string = approvalsContainer.name
