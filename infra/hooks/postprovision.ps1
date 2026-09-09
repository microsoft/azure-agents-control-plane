#!/usr/bin/env pwsh
# Post-provision hook for AKS setup

$ErrorActionPreference = "Stop"
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Write-Host "🔧 Post-provision setup..." -ForegroundColor Cyan

# Get environment values from azd
Write-Host "`n📝 Loading environment values..." -ForegroundColor Cyan
$envValues = azd env get-values | ConvertFrom-StringData

function Get-DeploymentValue([string]$Name) {
  # Match the existing property lookup: pipeline conversion can return several
  # hashtables, and PowerShell's member enumeration handles either shape.
  $value = $envValues.$Name
  if ($null -ne $value) {
    return ([string]$value).Trim().Trim('"')
  }
  return ''
}

$aksName = $envValues.AKS_CLUSTER_NAME.Trim('"')
$rgName = $envValues.AZURE_RESOURCE_GROUP_NAME.Trim('"')
$subscriptionId = $envValues.AZURE_SUBSCRIPTION_ID.Trim('"')
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
$mcpAgentRuntime = if ($envValues.MCP_AGENT_RUNTIME) { $envValues.MCP_AGENT_RUNTIME.Trim('"').Trim().ToLowerInvariant() } else { 'python' }
if ($mcpAgentRuntime -notin @('python', 'typescript')) {
  Write-Host "Unsupported MCP_AGENT_RUNTIME '$mcpAgentRuntime'. Use 'python' or 'typescript'." -ForegroundColor Red
  exit 1
}
$imageTag = if ($mcpAgentRuntime -eq 'typescript') { 'typescript' } else { 'latest' }
$tenantId = Get-DeploymentValue 'AZURE_TENANT_ID'
$agentIdentityEnabled = (Get-DeploymentValue 'AGENT_IDENTITY_ENABLED').ToLowerInvariant()
if (-not $agentIdentityEnabled) { $agentIdentityEnabled = 'false' }
$agentRegistryEnabled = (Get-DeploymentValue 'AGENT_REGISTRY_ENABLED').ToLowerInvariant()
if (-not $agentRegistryEnabled) { $agentRegistryEnabled = 'false' }
$approvalLogicAppEnabled = (Get-DeploymentValue 'APPROVAL_LOGIC_APP_ENABLED').ToLowerInvariant()
if (-not $approvalLogicAppEnabled) { $approvalLogicAppEnabled = 'false' }
if ($agentIdentityEnabled -notin @('true', 'false') -or $agentRegistryEnabled -notin @('true', 'false') -or $approvalLogicAppEnabled -notin @('true', 'false')) {
  throw 'AGENT_IDENTITY_ENABLED, AGENT_REGISTRY_ENABLED and APPROVAL_LOGIC_APP_ENABLED must be true or false.'
}
if ($agentRegistryEnabled -eq 'true' -and $agentIdentityEnabled -ne 'true') {
  throw 'Agent Registry publication requires AGENT_IDENTITY_ENABLED=true.'
}
$agentIdentityAppId = Get-DeploymentValue 'AGENT_IDENTITY_APP_ID'
$agentBlueprintAppId = Get-DeploymentValue 'AGENT_IDENTITY_BLUEPRINT_APP_ID'
$agentBlueprintObjectId = Get-DeploymentValue 'AGENT_IDENTITY_BLUEPRINT_OBJECT_ID'
if ($agentIdentityEnabled -eq 'true' -and (-not $agentIdentityAppId -or -not $agentBlueprintAppId)) {
  throw 'Enabled Agent Identity requires its actual app and blueprint IDs; the bootstrap UAMI is not a substitute.'
}
if ($agentRegistryEnabled -eq 'true' -and -not $agentBlueprintObjectId) {
  throw 'Agent Registry publication requires AGENT_IDENTITY_BLUEPRINT_OBJECT_ID.'
}
foreach ($id in @($agentIdentityAppId, $agentBlueprintAppId, $agentBlueprintObjectId)) {
  if ($id -and $id -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw 'Invalid Agent Identity app or blueprint ID.'
  }
}
$agentRegistryApi = (Get-DeploymentValue 'AGENT_REGISTRY_API').ToLowerInvariant()
if (-not $agentRegistryApi) { $agentRegistryApi = 'agent365' }
if ($agentRegistryApi -notin @('agent365', 'entra-beta')) {
  throw 'AGENT_REGISTRY_API must be agent365 or entra-beta.'
}
$agentRegistryOwnerIds = @((Get-DeploymentValue 'AGENT_REGISTRY_OWNER_IDS') -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($agentRegistryEnabled -eq 'true' -and $agentRegistryOwnerIds.Count -eq 0) {
  throw 'Agent Registry publication requires AGENT_REGISTRY_OWNER_IDS.'
}
foreach ($id in $agentRegistryOwnerIds) {
  if ($id -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw 'AGENT_REGISTRY_OWNER_IDS must contain comma-separated GUIDs.'
  }
}
$agentEndpointUrl = Get-DeploymentValue 'MCP_BASE_URL'
$agentIdentityDisplayName = Get-DeploymentValue 'AGENT_IDENTITY_DISPLAY_NAME'
$deploymentEnvironment = Get-DeploymentValue 'DEPLOYMENT_ENVIRONMENT'
if (-not $deploymentEnvironment) { $deploymentEnvironment = Get-DeploymentValue 'AZURE_ENV_NAME' }
$commitSha = Get-DeploymentValue 'COMMIT_SHA'
if (-not $commitSha) { $commitSha = [string]$env:COMMIT_SHA }
if ($commitSha -and $commitSha -notmatch '^[0-9a-fA-F]{7,64}$') {
  throw 'COMMIT_SHA must be empty or a hexadecimal commit ID.'
}
if ($approvalLogicAppEnabled -eq 'true' -or $agentRegistryEnabled -eq 'true') {
  $deploymentPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $deploymentPython) {
    throw 'Approval and registry deployment helpers require Python 3.10+ as python on PATH; configure it separately. No interpreter is installed by this step.'
  }
}
# Fabric configuration
$fabricEnabled = 'false'
$fabricCapacityName = $envValues.FABRIC_CAPACITY_NAME.Trim('"')
$fabricOneLakeDfsEndpoint = ''
$fabricOneLakeBlobEndpoint = ''

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
Write-Host "  MCP Agent Runtime: $mcpAgentRuntime" -ForegroundColor White
Write-Host "  Fabric Enabled: $fabricEnabled" -ForegroundColor White
Write-Host "  Fabric Capacity: $fabricCapacityName" -ForegroundColor White
Write-Host "  OneLake DFS Endpoint: $fabricOneLakeDfsEndpoint" -ForegroundColor White

if (-not $aksName -or -not $rgName) {
  Write-Host "⚠️  Could not find AKS cluster name or resource group" -ForegroundColor Yellow
  exit 1
}

az account set --subscription $subscriptionId
if ($LASTEXITCODE -ne 0) {
  throw "Failed to select Azure subscription $subscriptionId"
}

# Get AKS credentials
Write-Host "`n🔑 Getting AKS credentials..." -ForegroundColor Cyan
az aks get-credentials --resource-group $rgName --name $aksName --overwrite-existing --admin
if ($LASTEXITCODE -ne 0) { throw "Failed to get AKS credentials" }
Write-Host "✅ AKS credentials configured" -ForegroundColor Green

# Grant current user AKS RBAC access
Write-Host "`n🔐 Granting AKS RBAC access..." -ForegroundColor Cyan
$userId = az ad signed-in-user show --query id -o tsv
$aksResourceId = az aks show --resource-group $rgName --name $aksName --query id -o tsv
az role assignment create --role "Azure Kubernetes Service RBAC Cluster Admin" --assignee $userId --scope $aksResourceId 2>$null
if ($LASTEXITCODE -ne 0) { throw "Failed to grant AKS RBAC access" }
Write-Host "✅ RBAC access granted" -ForegroundColor Green

# Attach ACR to AKS
Write-Host "`n🔗 Attaching ACR to AKS..." -ForegroundColor Cyan
$acrName = $containerRegistry -replace '\.azurecr\.io$', ''
az aks update --resource-group $rgName --name $aksName --attach-acr $acrName
if ($LASTEXITCODE -ne 0) { throw "Failed to attach ACR to AKS" }
Write-Host "✅ ACR attached" -ForegroundColor Green

# Configure Kubernetes deployment files
Write-Host "`n📄 Configuring Kubernetes manifests..." -ForegroundColor Cyan

# Read and configure deployment template
$deploymentTemplate = Get-Content -Path "./k8s/mcp-agents-deployment.yaml" -Raw
$configuredDeployment = $deploymentTemplate `
  -replace '\$\{CONTAINER_REGISTRY\}', $containerRegistry `
  -replace '\$\{IMAGE_TAG\}', $imageTag `
  -replace '\$\{COMMIT_SHA\}', $commitSha `
  -replace '\$\{AZURE_STORAGE_ACCOUNT_URL\}', $storageUrl `
  -replace '\$\{AZURE_CLIENT_ID\}', $mcpIdentityClientId `
  -replace '\$\{AZURE_TENANT_ID\}', $tenantId `
  -replace '\$\{MCP_SERVER_IDENTITY_CLIENT_ID\}', $mcpIdentityClientId `
  -replace '\$\{AGENT_IDENTITY_ENABLED\}', $agentIdentityEnabled `
  -replace '\$\{AGENT_IDENTITY_APP_ID\}', $agentIdentityAppId `
  -replace '\$\{AGENT_IDENTITY_BLUEPRINT_APP_ID\}', $agentBlueprintAppId `
  -replace '\$\{AGENT_IDENTITY_DISPLAY_NAME\}', $agentIdentityDisplayName `
  -replace '\$\{FOUNDRY_PROJECT_ENDPOINT\}', $foundryProjectEndpoint `
  -replace '\$\{FOUNDRY_MODEL_DEPLOYMENT_NAME\}', $foundryModelDeploymentName `
  -replace '\$\{EMBEDDING_MODEL_DEPLOYMENT_NAME\}', $embeddingModelDeploymentName `
  -replace '\$\{COSMOSDB_ENDPOINT\}', $cosmosDbEndpoint `
  -replace '\$\{COSMOSDB_DATABASE_NAME\}', $cosmosDbDatabaseName `
  -replace '\$\{AGENT_LEARNING_STORE_BACKEND:-cosmos\}', 'cosmos' `
  -replace '\$\{AGENT_LEARNING_ENABLE_CAPTURE:-false\}', 'false' `
  -replace '\$\{AZURE_SEARCH_ENDPOINT\}', $azureSearchEndpoint `
  -replace '\$\{AZURE_SEARCH_INDEX_NAME\}', $azureSearchIndexName `
  -replace '\$\{AZURE_SEARCH_KNOWLEDGE_BASE_NAME\}', 'task-instructions-kb' `
  -replace '\$\{FABRIC_ENABLED\}', $fabricEnabled `
  -replace '\$\{FABRIC_ENDPOINT\}', $fabricOneLakeDfsEndpoint `
  -replace '\$\{FABRIC_WORKSPACE_ID\}', '' `
  -replace '\$\{FABRIC_ONTOLOGY_NAME\}', 'agent-ontology' `
  -replace '\$\{FABRIC_ONELAKE_DFS_ENDPOINT\}', $fabricOneLakeDfsEndpoint `
  -replace '\$\{FABRIC_ONELAKE_BLOB_ENDPOINT\}', $fabricOneLakeBlobEndpoint `
  -replace '\$\{FABRIC_LAKEHOUSE_NAME\}', 'mcpontologies' `
  -replace '\$\{FABRIC_ONTOLOGY_PATH\}', 'Files/ontology' `
  -replace '\$\{FABRIC_API_ENDPOINT\}', '' `
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
$existingCred = az identity federated-credential list `
  --identity-name $identityName `
  --resource-group $rgName `
  --query "[?name == 'mcp-agents-federated'].name | [0]" `
  --output tsv
if (-not $existingCred) {
  az identity federated-credential create `
    --name mcp-agents-federated `
    --identity-name $identityName `
    --resource-group $rgName `
    --issuer $oidcIssuer `
    --subject "system:serviceaccount:mcp-agents:mcp-agents-sa" `
    --audiences "api://AzureADTokenExchange"
  if ($LASTEXITCODE -ne 0) { throw "Failed to create the workload identity credential" }
  Write-Host "✅ Federated identity credential created" -ForegroundColor Green
} else {
  Write-Host "✅ Federated identity credential already exists" -ForegroundColor Green
}

# Build and push container image
Write-Host "`n🐳 Building and pushing container image..." -ForegroundColor Cyan
$env:CONTAINER_REGISTRY = $containerRegistry
$env:MCP_AGENT_RUNTIME = $mcpAgentRuntime
$env:IMAGE_TAG = $imageTag
& "./scripts/build-and-push.ps1"
if ($LASTEXITCODE -ne 0) { throw "Container image build failed" }

# Deploy to Kubernetes
Write-Host "`n🚀 Deploying to Kubernetes..." -ForegroundColor Cyan
# Inject approval runtime only after the namespace exists and before any rollout.
if ($approvalLogicAppEnabled -eq 'true') {
  '{"apiVersion":"v1","kind":"Namespace","metadata":{"name":"mcp-agents"}}' | kubectl apply -f -
  if ($LASTEXITCODE -ne 0) { throw 'Failed to ensure the approval namespace exists.' }
  # The helper reads grouped or legacy non-secret outputs as JSON. It alone
  # handles listCallbackUrl and streams the signed URL to Kubernetes Secret stdin.
  & $deploymentPython.Source "./scripts/configure_approval_runtime.py" --apply --from-azd `
    "--subscription-id=$subscriptionId" `
    "--resource-group=$rgName" `
    "--namespace=mcp-agents" `
    "--tenant-id=$tenantId" `
    "--environment=$deploymentEnvironment" `
    "--cluster-name=$aksName"
  if ($LASTEXITCODE -ne 0) { throw 'Approval runtime configuration failed; rollout was not started.' }
}
kubectl apply -f ./k8s/mcp-agents-deployment-configured.yaml
if ($LASTEXITCODE -ne 0) { throw "Failed to apply the MCP deployment" }
kubectl apply -f ./k8s/mcp-agents-loadbalancer-configured.yaml
if ($LASTEXITCODE -ne 0) { throw "Failed to apply the MCP load balancer" }
kubectl rollout restart deployment/mcp-agents -n mcp-agents
if ($LASTEXITCODE -ne 0) { throw "Failed to restart the MCP deployment" }

# Wait for deployment to be ready
Write-Host "`n⏳ Waiting for deployment to be ready..." -ForegroundColor Cyan
kubectl rollout status deployment/mcp-agents -n mcp-agents --timeout=300s
if ($LASTEXITCODE -ne 0) { throw "MCP deployment rollout failed" }

if ($agentRegistryEnabled -eq 'true') {
  if (-not $agentEndpointUrl -or -not $agentIdentityDisplayName -or -not $deploymentEnvironment) {
    throw 'Agent Registry publication requires MCP_BASE_URL, AGENT_IDENTITY_DISPLAY_NAME and AZURE_ENV_NAME deployment values.'
  }
  Write-Host "`n📇 Publishing agent to the Agent 365 registry..." -ForegroundColor Cyan
  $registryArguments = @(
    './scripts/publish_agent_registry.py', '--publish', "--api=$agentRegistryApi",
    "--endpoint=$agentEndpointUrl", "--agent-identity-id=$agentIdentityAppId",
    "--blueprint-object-id=$agentBlueprintObjectId",
    "--display-name=$agentIdentityDisplayName", "--tenant-id=$tenantId"
  )
  foreach ($ownerId in $agentRegistryOwnerIds) { $registryArguments += "--owner-id=$ownerId" }
  $env:AGENT_REGISTRY_ENABLED = 'true'
  $env:AZURE_ENV_NAME = $deploymentEnvironment
  & $deploymentPython.Source @registryArguments
  if ($LASTEXITCODE -ne 0) { throw 'Agent 365 registry publication failed.' }
  Write-Host "✅ Agent 365 registry publication complete" -ForegroundColor Green
}

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
