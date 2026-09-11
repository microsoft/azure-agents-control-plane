// Stage 1 only. Invoke through scripts/deploy_agent_identity.py so permission
// preflight and verified output handoff cannot be confused with ARM success.
// No AKS, ACR, network, Logic App, Teams connection, database or container writes.
// Graph consent is an administrator prerequisite, never a deployment operation.
targetScope = 'resourceGroup'

@allowed(['blueprint', 'identity'])
param phase string = 'blueprint'

param location string = resourceGroup().location
param managedIdentityName string
param blueprintDisplayName string
param blueprintUniqueName string
param agentDisplayName string
param sponsorPrincipalId string
param existingBlueprintAppId string = ''
param existingAgentIdentityId string = ''
param forceUpdateTag string = 'identity-v2'

// Names of EXISTING services. Only their role-assignment resources are written.
// Blueprint-only bootstrap does not create Azure role assignments either.
param cosmosDbAccountName string = ''
param storageAccountName string = ''
param foundryAccountName string = ''
param searchServiceName string = ''

resource configurationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: managedIdentityName
}

module blueprint './core/identity/agentIdentityBlueprint.bicep' = {
  name: 'scoped-agent-blueprint'
  params: {
    location: location
    blueprintDisplayName: blueprintDisplayName
    blueprintUniqueName: blueprintUniqueName
    managedIdentityResourceId: configurationIdentity.id
    federatedIdentityPrincipalId: configurationIdentity.properties.principalId
    existingBlueprintAppId: existingBlueprintAppId
    sponsorPrincipalIds: [sponsorPrincipalId]
    ownerPrincipalIds: [sponsorPrincipalId]
    agentScopeValue: 'next_best_action'
    forceUpdateTag: forceUpdateTag
  }
}

module agent './core/identity/agentIdentity.bicep' = if (phase == 'identity') {
  name: 'scoped-agent-identity'
  params: {
    location: location
    agentDisplayName: agentDisplayName
    blueprintAppId: blueprint.outputs.blueprintAppId
    managedIdentityResourceId: configurationIdentity.id
    existingAgentIdentityId: existingAgentIdentityId
    sponsorPrincipalIds: [sponsorPrincipalId]
    forceUpdateTag: forceUpdateTag
  }
}

module access './app/agent-RoleAssignments.bicep' = if (phase == 'identity') {
  name: 'scoped-agent-access'
  params: {
    agentPrincipalId: agent!.outputs.agentIdentityPrincipalId
    cosmosAccountName: cosmosDbAccountName
    storageAccountName: storageAccountName
    foundryAccountName: foundryAccountName
    searchServiceName: searchServiceName
    searchEnabled: !empty(searchServiceName)
  }
}

// Blueprint application object ID and blueprint service-principal ID are NOT
// interchangeable. Registry metadata uses the former; consent uses the latter.
output AGENT_IDENTITY_BLUEPRINT_APP_ID string = blueprint.outputs.blueprintAppId
output AGENT_IDENTITY_BLUEPRINT_OBJECT_ID string = blueprint.outputs.blueprintObjectId
output AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID string = blueprint.outputs.blueprintPrincipalId
output AGENT_IDENTITY_APP_ID string = phase == 'identity' ? agent!.outputs.agentIdentityAppId : ''
output AGENT_IDENTITY_PRINCIPAL_ID string = phase == 'identity' ? agent!.outputs.agentIdentityPrincipalId : ''
output AGENT_IDENTITY_DISPLAY_NAME string = phase == 'identity' ? agent!.outputs.agentDisplayName : ''
output AGENT_IDENTITY_CONFIGURATION_RESOURCE_ID string = configurationIdentity.id
output AGENT_IDENTITY_CONFIGURATION_PRINCIPAL_ID string = configurationIdentity.properties.principalId
output MCP_SERVER_IDENTITY_CLIENT_ID string = configurationIdentity.properties.clientId