// Role assignments for Next Best Action Agent Identity
// This module assigns all required roles to the agent identity for accessing Azure resources
// Note: Role assignment names use 'agent' suffix to differentiate from MCP identity assignments

@description('Principal ID of the agent identity')
param agentPrincipalId string

@description('Cosmos DB account name')
param cosmosAccountName string

@description('Azure AI Search service name')
param searchServiceName string

@description('Whether Azure AI Search is deployed and should receive Agent Identity role assignments')
param searchEnabled bool = true

@description('Storage account name')
param storageAccountName string

@description('Azure AI Foundry account name')
param foundryAccountName string

@description('Unique suffix for role assignment names to ensure idempotency')
param deploymentSuffix string = 'agent-v1'

// =========================================
// Built-in Role Definition IDs
// =========================================

// Cosmos DB Built-in Data Contributor (data plane access)
var CosmosDBDataContributor = '00000000-0000-0000-0000-000000000002'

// Azure AI Search roles
var SearchIndexDataContributor = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var SearchServiceContributor = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'

// Storage roles
var StorageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var StorageQueueDataContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'

// Azure AI / Cognitive Services roles
var CognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var CognitiveServicesOpenAIContributor = 'a001fd3d-188f-4b5d-821b-7da978bf7442'

// Note: Monitoring roles and Fabric roles are handled separately in main.bicep

// =========================================
// Cosmos DB Role Assignment
// =========================================

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource cosmosRoleAssignmentAgent 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  name: guid(cosmosAccount.id, agentPrincipalId, CosmosDBDataContributor, deploymentSuffix)
  parent: cosmosAccount
  properties: {
    principalId: agentPrincipalId
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${CosmosDBDataContributor}'
    scope: cosmosAccount.id
  }
}

// =========================================
// Azure AI Search Role Assignments
// =========================================

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = if (searchEnabled) {
  name: searchServiceName
}

// Search Index Data Contributor - read/write data in indexes
resource searchIndexDataContributorAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (searchEnabled) {
  name: guid(searchService.id, agentPrincipalId, SearchIndexDataContributor, deploymentSuffix)
  scope: searchService
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', SearchIndexDataContributor)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Search Service Contributor - manage indexes and knowledge bases
resource searchServiceContributorAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (searchEnabled) {
  name: guid(searchService.id, agentPrincipalId, SearchServiceContributor, deploymentSuffix)
  scope: searchService
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', SearchServiceContributor)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =========================================
// Storage Account Role Assignments
// =========================================

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

// Storage Blob Data Owner - full access to blob storage
resource storageBlobDataOwnerAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, agentPrincipalId, StorageBlobDataOwner, deploymentSuffix)
  scope: storageAccount
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', StorageBlobDataOwner)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Queue Data Contributor - for message processing
resource storageQueueDataContributorAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, agentPrincipalId, StorageQueueDataContributor, deploymentSuffix)
  scope: storageAccount
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', StorageQueueDataContributor)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =========================================
// Azure AI Foundry Role Assignments
// =========================================

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

// Cognitive Services OpenAI User - access to models (gpt-4o-mini)
resource openAIUserAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, agentPrincipalId, CognitiveServicesOpenAIUser, deploymentSuffix)
  scope: foundryAccount
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', CognitiveServicesOpenAIUser)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services OpenAI Contributor - manage deployments
resource openAIContributorAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, agentPrincipalId, CognitiveServicesOpenAIContributor, deploymentSuffix)
  scope: foundryAccount
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', CognitiveServicesOpenAIContributor)
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =========================================
// Outputs
// =========================================

output cosmosRoleAssignmentId string = cosmosRoleAssignmentAgent.id
output searchIndexRoleAssignmentId string = searchEnabled ? searchIndexDataContributorAgent!.id : ''
output searchServiceRoleAssignmentId string = searchEnabled ? searchServiceContributorAgent!.id : ''
output storageBlobRoleAssignmentId string = storageBlobDataOwnerAgent.id
output storageQueueRoleAssignmentId string = storageQueueDataContributorAgent.id
output foundryOpenAIUserRoleAssignmentId string = openAIUserAgent.id
output foundryOpenAIContributorRoleAssignmentId string = openAIContributorAgent.id
