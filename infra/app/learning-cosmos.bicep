// =========================================
// Azure Agents Learning SDK Cosmos DB Resources
// =========================================
// This module provisions the Cosmos DB database and containers required by the
// Azure Agents Learning SDK (https://github.com/microsoft/azure-agents-learning-sdk).
// The SDK runs an in-process reinforcement-learning loop and persists its five
// durable record types:
// - learning_episodes: Agent interactions (input, tool calls, output)
// - learning_rewards:  Judge/human rewards attached to episodes
// - learning_metrics:  Per-episode Azure AI Evaluation judge metric results
// - learning_policies: Versioned softmax policy snapshots
// - learning_runs:     Offline REINFORCE learning-run records
//
// See docs/AGENTS_AGENT_LEARNING_DESIGN.md for details

@description('Name of the parent Azure Cosmos DB account.')
param parentAccountName string

@description('Name of the Learning database (default: agent_learning).')
param databaseName string = 'agent_learning'

@description('Tags for all resources.')
param tags object = {}

// Reference to existing Cosmos account
resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: parentAccountName
}

// =========================================
// Learning Database
// =========================================
resource learningDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  name: databaseName
  parent: account
  tags: tags
  properties: {
    resource: {
      id: databaseName
    }
  }
}

// Container names (aligned with the SDK's AGENT_LEARNING_CONTAINER_* defaults)
var containerNames = [
  'learning_episodes'
  'learning_rewards'
  'learning_metrics'
  'learning_policies'
  'learning_runs'
]

// =========================================
// Learning containers
// All records are partitioned by agent_id (AGENT_LEARNING_PARTITION_KEY_FIELD).
// Episodes, rewards, metrics, policies and runs are long-lived learning data,
// so TTL is disabled (defaultTtl = -1).
// =========================================
resource learningContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [
  for name in containerNames: {
    name: name
    parent: learningDatabase
    tags: tags
    properties: {
      resource: {
        id: name
        partitionKey: {
          paths: ['/agent_id']
          kind: 'Hash'
          version: 2
        }
        indexingPolicy: {
          automatic: true
          indexingMode: 'consistent'
          includedPaths: [
            { path: '/*' }
          ]
          excludedPaths: [
            { path: '/"_etag"/?' }
          ]
        }
        // TTL disabled by default - learning records are long-lived
        defaultTtl: -1
      }
    }
  }
]

// =========================================
// Outputs
// =========================================
output databaseName string = learningDatabase.name
output containerNames array = containerNames
