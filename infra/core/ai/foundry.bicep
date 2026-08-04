@description('Name of the foundry resource')
param foundryName string

@description('Name of the Bing Grounding resource')
param bingName string

@description('Enable Bing Grounding (disable when Bing is unavailable/suspended for the subscription)')
param bingEnabled bool = true

@description('Location for all resources')
param location string

@description('Model deployment name')
param modelDeploymentName string

@description('Model name')
param modelName string

@description('Model version')
param modelVersion string

@description('Model capacity')
param modelCapacity int

@description('Evaluation/judge model deployment name (Azure AI Evaluation judges for the Learning SDK)')
param evalModelDeploymentName string = 'gpt-4o-mini'

@description('Evaluation/judge model name')
param evalModelName string = 'gpt-4o-mini'

@description('Evaluation/judge model version')
param evalModelVersion string = '2024-07-18'

@description('Evaluation/judge model capacity')
param evalModelCapacity int = 10

@description('Embedding model deployment name')
param embeddingModelDeploymentName string = 'text-embedding-3-large'

@description('Embedding model name')
param embeddingModelName string = 'text-embedding-3-large'

@description('Embedding model version')
param embeddingModelVersion string = '1'

@description('Embedding model capacity')
param embeddingModelCapacity int = 10

@description('Tags for resources')
param tags object = {}

@description('Enable private endpoint')
param enablePrivateEndpoint bool = false

@description('Public network access setting')
param publicNetworkAccess string = 'Enabled'

// Create Bing Grounding resource
resource bingGrounding 'Microsoft.Bing/accounts@2020-06-10' = if (bingEnabled) {
  name: bingName
  location: 'global'
  sku: {
    name: 'G1'
  }
  kind: 'Bing.Grounding'
  properties: {
    statisticsEnabled: false
  }
}

// Create AI Services foundry account
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    apiProperties: {}
    customSubDomainName: foundryName
    networkAcls: {
      defaultAction: enablePrivateEndpoint ? 'Deny' : 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    allowProjectManagement: true
    defaultProject: 'proj-default'
    associatedProjects: [
      'proj-default'
    ]
    publicNetworkAccess: publicNetworkAccess
    disableLocalAuth: true
  }
}

// Create Agents capability host
resource agentsCapabilityHost 'Microsoft.CognitiveServices/accounts/capabilityHosts@2025-06-01' = {
  parent: foundryAccount
  name: 'Agents'
  properties: {
    capabilityHostKind: 'Agents'
  }
}

// Deploy GPT model
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: modelCapacity
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// Deploy gpt-5.4-mini model for Azure AI Evaluation judges (Learning SDK reward signal)
resource evalModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: evalModelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: evalModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: evalModelName
      version: evalModelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: evalModelCapacity
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    modelDeployment
  ]
}

// Deploy text-embedding-3-large model for semantic similarity
resource embeddingModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: embeddingModelDeploymentName
  sku: {
    name: 'Standard'
    capacity: embeddingModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: embeddingModelCapacity
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    evalModelDeployment
  ]
}

// Create default project
resource defaultProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: 'proj-default'
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Default project for web summarization with Bing grounding'
    displayName: 'proj-default'
  }
}

// Create Bing connection at project level
resource bingConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = if (bingEnabled) {
  parent: defaultProject
  name: bingName
  properties: {
    authType: 'ApiKey'
    category: 'ApiKey'
    target: 'https://api.bing.microsoft.com/'
    credentials: {
      key: listKeys(bingGrounding.id, bingGrounding.apiVersion).key1
    }
    useWorkspaceManagedIdentity: false
    isSharedToAll: false
    sharedUserList: []
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    metadata: {
      type: 'bing_grounding'
      ApiType: 'Azure'
      ResourceId: bingGrounding.id
    }
  }
}

output foundryAccountId string = foundryAccount.id
output foundryAccountName string = foundryAccount.name
output foundryEndpoint string = foundryAccount.properties.endpoint
output projectId string = defaultProject.id
output projectEndpoint string = 'https://${foundryName}.services.ai.azure.com/api/projects/proj-default'
output bingConnectionId string = bingEnabled ? bingConnection.id : ''
output bingConnectionName string = bingEnabled ? bingConnection.name : ''
output bingResourceId string = bingEnabled ? bingGrounding.id : ''
output modelDeploymentName string = modelDeployment.name
output evalModelDeploymentName string = evalModelDeployment.name
output embeddingModelDeploymentName string = embeddingModelDeployment.name
