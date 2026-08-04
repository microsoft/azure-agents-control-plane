#!/usr/bin/env pwsh
# Post-provision hook for AKS setup

Write-Host "🔧 Post-provision setup..." -ForegroundColor Cyan

# Get environment values from azd
Write-Host "`n📝 Loading environment values..." -ForegroundColor Cyan
$envValues = azd env get-values | ConvertFrom-StringData -Delimiter '='

$aksName = $envValues.AKS_CLUSTER_NAME.Trim('"')
$rgName = $envValues.AZURE_RESOURCE_GROUP_NAME.Trim('"')
$containerRegistry = $envValues.CONTAINER_REGISTRY.Trim('"')
$storageUrl = $envValues.AZURE_STORAGE_ACCOUNT_URL.Trim('"')
$mcpIdentityClientId = $envValues.MCP_SERVER_IDENTITY_CLIENT_ID.Trim('"')
$mcpInternalLbIp = if ($envValues.MCP_INTERNAL_LB_IP) { $envValues.MCP_INTERNAL_LB_IP.Trim('"') } else { '10.0.4.4' }
$mcpLbSubnetName = if ($envValues.MCP_LB_SUBNET_NAME) { $envValues.MCP_LB_SUBNET_NAME.Trim('"') } else { 'svc-lb' }
$foundryProjectEndpoint = $envValues.FOUNDRY_PROJECT_ENDPOINT.Trim('"')
$foundryModelDeploymentName = $envValues.FOUNDRY_MODEL_DEPLOYMENT_NAME.Trim('"')
$embeddingModelDeploymentName = $envValues.EMBEDDING_MODEL_DEPLOYMENT_NAME.Trim('"')
$cosmosDbEndpoint = $envValues.COSMOSDB_ENDPOINT.Trim('"')
$cosmosDbDatabaseName = $envValues.COSMOSDB_DATABASE_NAME.Trim('"')
$azureSearchEndpoint = $envValues.AZURE_SEARCH_ENDPOINT.Trim('"')
$azureSearchIndexName = $envValues.AZURE_SEARCH_INDEX_NAME.Trim('"')
# Fabric configuration
$fabricEnabled = $envValues.FABRIC_ENABLED.Trim('"')
$fabricCapacityName = $envValues.FABRIC_CAPACITY_NAME.Trim('"')
$fabricOneLakeDfsEndpoint = $envValues.FABRIC_ONELAKE_DFS_ENDPOINT.Trim('"')
$fabricOneLakeBlobEndpoint = $envValues.FABRIC_ONELAKE_BLOB_ENDPOINT.Trim('"')

Write-Host "  AKS Cluster: $aksName" -ForegroundColor White
Write-Host "  Resource Group: $rgName" -ForegroundColor White
Write-Host "  Container Registry: $containerRegistry" -ForegroundColor White
Write-Host "  MCP Internal LB IP: $mcpInternalLbIp" -ForegroundColor White
Write-Host "  Foundry Endpoint: $foundryProjectEndpoint" -ForegroundColor White
Write-Host "  Foundry Model: $foundryModelDeploymentName" -ForegroundColor White
Write-Host "  Embedding Model: $embeddingModelDeploymentName" -ForegroundColor White
Write-Host "  CosmosDB Endpoint: $cosmosDbEndpoint" -ForegroundColor White
Write-Host "  CosmosDB Database: $cosmosDbDatabaseName" -ForegroundColor White
Write-Host "  AI Search Endpoint: $azureSearchEndpoint" -ForegroundColor White
Write-Host "  AI Search Index: $azureSearchIndexName" -ForegroundColor White
Write-Host "  Fabric Enabled: $fabricEnabled" -ForegroundColor White
Write-Host "  Fabric Capacity: $fabricCapacityName" -ForegroundColor White
Write-Host "  OneLake DFS Endpoint: $fabricOneLakeDfsEndpoint" -ForegroundColor White

if (-not $aksName -or -not $rgName) {
  Write-Host "⚠️  Could not find AKS cluster name or resource group" -ForegroundColor Yellow
  exit 1
}

# Get AKS credentials
Write-Host "`n🔑 Getting AKS credentials..." -ForegroundColor Cyan
az aks get-credentials --resource-group $rgName --name $aksName --overwrite-existing --admin
Write-Host "✅ AKS credentials configured" -ForegroundColor Green

# Grant current user AKS RBAC access
Write-Host "`n🔐 Granting AKS RBAC access..." -ForegroundColor Cyan
$userId = az ad signed-in-user show --query id -o tsv
$aksResourceId = az aks show --resource-group $rgName --name $aksName --query id -o tsv
az role assignment create --role "Azure Kubernetes Service RBAC Cluster Admin" --assignee $userId --scope $aksResourceId 2>$null
Write-Host "✅ RBAC access granted" -ForegroundColor Green

# Attach ACR to AKS
Write-Host "`n🔗 Attaching ACR to AKS..." -ForegroundColor Cyan
$acrName = $containerRegistry -replace '\.azurecr\.io$', ''
az aks update --resource-group $rgName --name $aksName --attach-acr $acrName
Write-Host "✅ ACR attached" -ForegroundColor Green

# Configure Kubernetes deployment files
Write-Host "`n📄 Configuring Kubernetes manifests..." -ForegroundColor Cyan

# Read and configure deployment template
$deploymentTemplate = Get-Content -Path "./k8s/mcp-agents-deployment.yaml" -Raw
$tenantId = $envValues.AZURE_TENANT_ID.Trim('"')
$configuredDeployment = $deploymentTemplate `
  -replace '\$\{CONTAINER_REGISTRY\}', $containerRegistry `
  -replace '\$\{IMAGE_TAG\}', 'latest' `
  -replace '\$\{AZURE_STORAGE_ACCOUNT_URL\}', $storageUrl `
  -replace '\$\{AZURE_CLIENT_ID\}', $mcpIdentityClientId `
  -replace '\$\{AZURE_TENANT_ID\}', $tenantId `
  -replace '\$\{MCP_SERVER_IDENTITY_CLIENT_ID\}', $mcpIdentityClientId `
  -replace '\$\{AGENT_IDENTITY_APP_ID\}', $mcpIdentityClientId `
  -replace '\$\{AGENT_IDENTITY_BLUEPRINT_APP_ID\}', '' `
  -replace '\$\{AGENT_IDENTITY_DISPLAY_NAME\}', 'MCP Agent' `
  -replace '\$\{FOUNDRY_PROJECT_ENDPOINT\}', $foundryProjectEndpoint `
  -replace '\$\{FOUNDRY_MODEL_DEPLOYMENT_NAME\}', $foundryModelDeploymentName `
  -replace '\$\{EMBEDDING_MODEL_DEPLOYMENT_NAME\}', $embeddingModelDeploymentName `
  -replace '\$\{COSMOSDB_ENDPOINT\}', $cosmosDbEndpoint `
  -replace '\$\{COSMOSDB_DATABASE_NAME\}', $cosmosDbDatabaseName `
  -replace '\$\{AZURE_SEARCH_ENDPOINT\}', $azureSearchEndpoint `
  -replace '\$\{AZURE_SEARCH_INDEX_NAME\}', $azureSearchIndexName `
  -replace '\$\{FABRIC_ENABLED\}', $fabricEnabled `
  -replace '\$\{FABRIC_ENDPOINT\}', $fabricOneLakeDfsEndpoint `
  -replace '\$\{FABRIC_WORKSPACE_ID\}', '' `
  -replace '\$\{FABRIC_ONTOLOGY_NAME\}', 'agent-ontology' `
  -replace '\$\{FABRIC_ONELAKE_DFS_ENDPOINT\}', $fabricOneLakeDfsEndpoint `
  -replace '\$\{FABRIC_ONELAKE_BLOB_ENDPOINT\}', $fabricOneLakeBlobEndpoint `
  -replace '\$\{FABRIC_LAKEHOUSE_NAME\}', 'mcpontologies' `
  -replace '\$\{FABRIC_ONTOLOGY_PATH\}', 'Files/ontology' `
  -replace '\$\{ONTOLOGY_CONTAINER_NAME\}', 'ontologies'
$configuredDeployment | Out-File -FilePath "./k8s/mcp-agents-deployment-configured.yaml" -Encoding utf8
Write-Host "  ✅ Configured mcp-agents-deployment-configured.yaml" -ForegroundColor Green

# Read and configure loadbalancer template (internal / private LoadBalancer)
$lbTemplate = Get-Content -Path "./k8s/mcp-agents-loadbalancer.yaml" -Raw
$configuredLb = $lbTemplate `
  -replace '\$\{AZURE_RESOURCE_GROUP_NAME\}', $rgName `
  -replace '\$\{MCP_INTERNAL_LB_IP\}', $mcpInternalLbIp `
  -replace '\$\{MCP_LB_SUBNET_NAME\}', $mcpLbSubnetName
$configuredLb | Out-File -FilePath "./k8s/mcp-agents-loadbalancer-configured.yaml" -Encoding utf8
Write-Host "  ✅ Configured mcp-agents-loadbalancer-configured.yaml" -ForegroundColor Green

# Create federated identity for workload identity
Write-Host "`n🔐 Configuring workload identity..." -ForegroundColor Cyan
$oidcIssuer = az aks show --resource-group $rgName --name $aksName --query "oidcIssuerProfile.issuerUrl" -o tsv
$identityName = "id-mcp-" + ($aksName -replace '^aks-', '')

# Check if federated credential already exists
$existingCred = az identity federated-credential show --name mcp-agents-federated --identity-name $identityName --resource-group $rgName 2>$null
if (-not $existingCred) {
  az identity federated-credential create `
    --name mcp-agents-federated `
    --identity-name $identityName `
    --resource-group $rgName `
    --issuer $oidcIssuer `
    --subject "system:serviceaccount:mcp-agents:mcp-agents-sa" `
    --audience "api://AzureADTokenExchange"
  Write-Host "✅ Federated identity credential created" -ForegroundColor Green
} else {
  Write-Host "✅ Federated identity credential already exists" -ForegroundColor Green
}

# Build and push container image
Write-Host "`n🐳 Building and pushing container image..." -ForegroundColor Cyan
$env:CONTAINER_REGISTRY = $containerRegistry
& "./scripts/build-and-push.ps1"

# Deploy to Kubernetes
Write-Host "`n🚀 Deploying to Kubernetes..." -ForegroundColor Cyan
kubectl apply -f ./k8s/mcp-agents-deployment-configured.yaml
kubectl apply -f ./k8s/mcp-agents-loadbalancer-configured.yaml

# Wait for deployment to be ready
Write-Host "`n⏳ Waiting for deployment to be ready..." -ForegroundColor Cyan
kubectl rollout status deployment/mcp-agents -n mcp-agents --timeout=300s

# Wait for LoadBalancer to get external IP
Write-Host "`n⏳ Waiting for LoadBalancer IP assignment..." -ForegroundColor Cyan
$maxRetries = 30
$retry = 0
$lbReady = $false
while ($retry -lt $maxRetries -and -not $lbReady) {
  $lbStatus = kubectl get svc mcp-agents-loadbalancer -n mcp-agents -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
  if ($lbStatus -eq $mcpInternalLbIp) {
    $lbReady = $true
    Write-Host "✅ Internal LoadBalancer ready with private IP: $lbStatus" -ForegroundColor Green
  } else {
    Write-Host "  Waiting for LoadBalancer... ($retry/$maxRetries)" -ForegroundColor Yellow
    Start-Sleep -Seconds 10
    $retry++
  }
}

if (-not $lbReady) {
  Write-Host "⚠️  LoadBalancer IP assignment timed out" -ForegroundColor Yellow
}

# Provision AI Search index & ingest task instructions
Write-Host "`n📚 Provisioning AI Search index & ingesting task instructions..." -ForegroundColor Cyan
try {
  # Ensure current user can create indexes (useful if running script locally)
  $searchServiceName = $envValues.AZURE_SEARCH_SERVICE_NAME.Trim('"')
  if (-not $searchServiceName) { throw "AI Search disabled (SEARCH_ENABLED=false); skipping index provisioning and task ingestion" }
  if ($searchServiceName) {
    $subscriptionId = az account show --query id -o tsv
    $searchScope = "/subscriptions/$subscriptionId/resourceGroups/$rgName/providers/Microsoft.Search/searchServices/$searchServiceName"

    Write-Host "  🔐 Assigning Search roles to signed-in user..." -ForegroundColor White
    az role assignment create --assignee $userId --role "Search Service Contributor" --scope $searchScope 2>$null | Out-Null
    az role assignment create --assignee $userId --role "Search Index Data Contributor" --scope $searchScope 2>$null | Out-Null
    Write-Host "  ✅ Search roles assigned (or already present)" -ForegroundColor Green
  }

  # Set environment variables for ingestion script
  $env:AZURE_SEARCH_ENDPOINT = $azureSearchEndpoint
  $env:AZURE_SEARCH_INDEX_NAME = $azureSearchIndexName
  $env:FOUNDRY_PROJECT_ENDPOINT = $foundryProjectEndpoint
  $env:EMBEDDING_MODEL_DEPLOYMENT_NAME = $embeddingModelDeploymentName

  # Install required Python packages (idempotent)
  Write-Host "  📦 Ensuring python dependencies..." -ForegroundColor White
  python -m pip install --disable-pip-version-check --quiet -r ./src/requirements.txt 2>$null

  # Run ingestion script (creates index if missing & uploads task_instructions/*.json)
  Write-Host "  🚀 Running ingestion script..." -ForegroundColor White
  python ./scripts/ingest_task_instructions.py
  Write-Host "  ✅ AI Search index provisioned & task instructions ingested" -ForegroundColor Green
}
catch {
  Write-Host "  ⚠️ AI Search provisioning/ingestion failed: $($_.Exception.Message)" -ForegroundColor Yellow
  Write-Host "  👉 You can rerun manually: python ./scripts/ingest_task_instructions.py" -ForegroundColor Yellow
}

# Generate test configuration
Write-Host "`n📝 Generating test configuration..." -ForegroundColor Cyan
& "./scripts/generate-test-config.ps1"

Write-Host "`n" -NoNewline
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "🎉 Post-provision setup complete!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan

# Fabric IQ Setup Instructions
if ($fabricEnabled -eq "true") {
  Write-Host "`n🧠 Microsoft Fabric IQ Setup:" -ForegroundColor Magenta
  Write-Host "  1. Create a Fabric Workspace in the Fabric portal" -ForegroundColor White
  Write-Host "  2. Create a Lakehouse named 'mcpontologies'" -ForegroundColor White
  Write-Host "  3. Upload ontologies to OneLake:" -ForegroundColor White
  Write-Host "     ./scripts/upload-ontologies-to-onelake.ps1 -WorkspaceId <GUID> -LakehouseName mcpontologies" -ForegroundColor Yellow
  Write-Host "  4. Configure Fabric IQ with the uploaded ontologies" -ForegroundColor White
  Write-Host "  5. Update FABRIC_WORKSPACE_ID environment variable" -ForegroundColor White
}

Write-Host "`n📝 Run integration tests:" -ForegroundColor Cyan
Write-Host "   python tests/test_apim_mcp_connection.py --use-az-token" -ForegroundColor White
Write-Host ""
