#!/bin/bash
set -e

# Post-provision hook for AKS setup

echo "🔧 Post-provision setup..."

# Get environment values from azd
echo ""
echo "📝 Loading environment values..."
eval $(azd env get-values | sed 's/^/export /')

az account set --subscription "$(echo "$AZURE_SUBSCRIPTION_ID" | tr -d '"')"

AKS_NAME=$(echo $AKS_CLUSTER_NAME | tr -d '"')
RG_NAME=$(echo $AZURE_RESOURCE_GROUP_NAME | tr -d '"')
CONTAINER_REG=$(echo $CONTAINER_REGISTRY | tr -d '"')
STORAGE_URL=$(echo $AZURE_STORAGE_ACCOUNT_URL | tr -d '"')
MCP_IDENTITY_CLIENT_ID=$(echo $MCP_SERVER_IDENTITY_CLIENT_ID | tr -d '"')
MCP_INTERNAL_LB_IP=$(echo ${MCP_INTERNAL_LB_IP:-10.0.4.4} | tr -d '"')
MCP_LB_SUBNET_NAME=$(echo ${MCP_LB_SUBNET_NAME:-svc-lb} | tr -d '"')
FOUNDRY_ENDPOINT=$(echo $FOUNDRY_PROJECT_ENDPOINT | tr -d '"')
FOUNDRY_MODEL=$(echo $FOUNDRY_MODEL_DEPLOYMENT_NAME | tr -d '"')
EMBEDDING_MODEL=$(echo $EMBEDDING_MODEL_DEPLOYMENT_NAME | tr -d '"')
COSMOS_ENDPOINT=$(echo $COSMOSDB_ENDPOINT | tr -d '"')
COSMOS_DATABASE=$(echo $COSMOSDB_DATABASE_NAME | tr -d '"')
SEARCH_ENDPOINT=$(echo $AZURE_SEARCH_ENDPOINT | tr -d '"')
SEARCH_INDEX=$(echo $AZURE_SEARCH_INDEX_NAME | tr -d '"')
MCP_AGENT_RUNTIME=$(echo "${MCP_AGENT_RUNTIME:-python}" | tr -d '"' | tr '[:upper:]' '[:lower:]')
case "$MCP_AGENT_RUNTIME" in
  python)
    IMAGE_TAG="latest"
    ;;
  typescript)
    IMAGE_TAG="typescript"
    ;;
  *)
    echo "Unsupported MCP_AGENT_RUNTIME '$MCP_AGENT_RUNTIME'. Use 'python' or 'typescript'."
    exit 1
    ;;
esac

echo "  AKS Cluster: $AKS_NAME"
echo "  Resource Group: $RG_NAME"
echo "  Container Registry: $CONTAINER_REG"
echo "  MCP Internal LB IP: $MCP_INTERNAL_LB_IP"
echo "  Foundry Endpoint: $FOUNDRY_ENDPOINT"
echo "  Foundry Model: $FOUNDRY_MODEL"
echo "  Embedding Model: $EMBEDDING_MODEL"
echo "  CosmosDB Endpoint: $COSMOS_ENDPOINT"
echo "  CosmosDB Database: $COSMOS_DATABASE"
echo "  AI Search Endpoint: $SEARCH_ENDPOINT"
echo "  AI Search Index: $SEARCH_INDEX"
echo "  MCP Agent Runtime: $MCP_AGENT_RUNTIME"

if [ -z "$AKS_NAME" ] || [ -z "$RG_NAME" ]; then
  echo "⚠️  Could not find AKS cluster name or resource group"
  exit 1
fi

# Get AKS credentials
echo ""
echo "🔑 Getting AKS credentials..."
az aks get-credentials --resource-group "$RG_NAME" --name "$AKS_NAME" --overwrite-existing --admin
echo "✅ AKS credentials configured"

# Grant current user AKS RBAC access
echo ""
echo "🔐 Granting AKS RBAC access..."
USER_ID=$(az ad signed-in-user show --query id -o tsv)
AKS_RESOURCE_ID=$(az aks show --resource-group "$RG_NAME" --name "$AKS_NAME" --query id -o tsv)
az role assignment create --role "Azure Kubernetes Service RBAC Cluster Admin" --assignee "$USER_ID" --scope "$AKS_RESOURCE_ID" 2>/dev/null || true
echo "✅ RBAC access granted"

# Attach ACR to AKS
echo ""
echo "🔗 Attaching ACR to AKS..."
ACR_NAME=$(echo "$CONTAINER_REG" | sed 's/\.azurecr\.io$//')
az aks update --resource-group "$RG_NAME" --name "$AKS_NAME" --attach-acr "$ACR_NAME"
echo "✅ ACR attached"

# Configure Kubernetes deployment files
echo ""
echo "📄 Configuring Kubernetes manifests..."

# Read and configure deployment template
sed -e "s|\${CONTAINER_REGISTRY}|$CONTAINER_REG|g" \
  -e "s|\${IMAGE_TAG}|$IMAGE_TAG|g" \
    -e "s|\${AZURE_STORAGE_ACCOUNT_URL}|$STORAGE_URL|g" \
    -e "s|\${AZURE_CLIENT_ID}|$MCP_IDENTITY_CLIENT_ID|g" \
    -e "s|\${FOUNDRY_PROJECT_ENDPOINT}|$FOUNDRY_ENDPOINT|g" \
    -e "s|\${FOUNDRY_MODEL_DEPLOYMENT_NAME}|$FOUNDRY_MODEL|g" \
    -e "s|\${EMBEDDING_MODEL_DEPLOYMENT_NAME}|$EMBEDDING_MODEL|g" \
    -e "s|\${COSMOSDB_ENDPOINT}|$COSMOS_ENDPOINT|g" \
    -e "s|\${COSMOSDB_DATABASE_NAME}|$COSMOS_DATABASE|g" \
    -e "s|\${AGENT_LEARNING_STORE_BACKEND:-cosmos}|cosmos|g" \
    -e "s|\${AGENT_LEARNING_ENABLE_CAPTURE:-false}|false|g" \
    -e "s|\${AZURE_SEARCH_ENDPOINT}|$SEARCH_ENDPOINT|g" \
    -e "s|\${AZURE_SEARCH_INDEX_NAME}|$SEARCH_INDEX|g" \
    -e "s|\${AZURE_SEARCH_KNOWLEDGE_BASE_NAME}|task-instructions-kb|g" \
    -e "s|\${FABRIC_API_ENDPOINT}||g" \
    ./k8s/mcp-agents-deployment.yaml > ./k8s/mcp-agents-deployment-configured.yaml
echo "  ✅ Configured mcp-agents-deployment-configured.yaml"

# Read and configure loadbalancer template (internal / private LoadBalancer)
sed -e "s|\${AZURE_RESOURCE_GROUP_NAME}|$RG_NAME|g" \
    -e "s|\${MCP_INTERNAL_LB_IP}|$MCP_INTERNAL_LB_IP|g" \
    -e "s|\${MCP_LB_SUBNET_NAME}|$MCP_LB_SUBNET_NAME|g" \
    ./k8s/mcp-agents-loadbalancer.yaml > ./k8s/mcp-agents-loadbalancer-configured.yaml
echo "  ✅ Configured mcp-agents-loadbalancer-configured.yaml"

# Create federated identity for workload identity
echo ""
echo "🔐 Configuring workload identity..."
OIDC_ISSUER=$(az aks show --resource-group "$RG_NAME" --name "$AKS_NAME" --query "oidcIssuerProfile.issuerUrl" -o tsv)
IDENTITY_NAME="id-mcp-${AKS_NAME#aks-}"

# Check if federated credential already exists
if ! az identity federated-credential show --name mcp-agents-federated --identity-name "$IDENTITY_NAME" --resource-group "$RG_NAME" 2>/dev/null; then
  az identity federated-credential create \
    --name mcp-agents-federated \
    --identity-name "$IDENTITY_NAME" \
    --resource-group "$RG_NAME" \
    --issuer "$OIDC_ISSUER" \
    --subject "system:serviceaccount:mcp-agents:mcp-agents-sa" \
    --audience "api://AzureADTokenExchange"
  echo "✅ Federated identity credential created"
else
  echo "✅ Federated identity credential already exists"
fi

# Build and push container image
echo ""
echo "🐳 Building and pushing container image..."
export CONTAINER_REGISTRY="$CONTAINER_REG"
export MCP_AGENT_RUNTIME
export IMAGE_TAG
./scripts/build-and-push.sh

# Deploy to Kubernetes
echo ""
echo "🚀 Deploying to Kubernetes..."
kubectl apply -f ./k8s/mcp-agents-deployment-configured.yaml
kubectl apply -f ./k8s/mcp-agents-loadbalancer-configured.yaml
kubectl rollout restart deployment/mcp-agents -n mcp-agents

# Wait for deployment to be ready
echo ""
echo "⏳ Waiting for deployment to be ready..."
kubectl rollout status deployment/mcp-agents -n mcp-agents --timeout=300s

# Wait for LoadBalancer to get its private IP
echo ""
echo "⏳ Waiting for LoadBalancer IP assignment..."
MAX_RETRIES=30
RETRY=0
LB_READY=false
while [ $RETRY -lt $MAX_RETRIES ] && [ "$LB_READY" = "false" ]; do
  LB_STATUS=$(kubectl get svc mcp-agents-loadbalancer -n mcp-agents -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
  if [ "$LB_STATUS" = "$MCP_INTERNAL_LB_IP" ]; then
    LB_READY=true
    echo "✅ Internal LoadBalancer ready with private IP: $LB_STATUS"
  else
    echo "  Waiting for LoadBalancer... ($RETRY/$MAX_RETRIES)"
    sleep 10
    RETRY=$((RETRY + 1))
  fi
done

if [ "$LB_READY" = "false" ]; then
  echo "⚠️  LoadBalancer IP assignment timed out"
fi

# Provision AI Search index & ingest task instructions
echo ""
echo "📚 Provisioning AI Search index & ingesting task instructions..."
try_run_ai_search() {
  if [[ -n "$AZURE_SEARCH_SERVICE_NAME" ]]; then
    subscription_id=$(az account show --query id -o tsv)
    search_scope="/subscriptions/${subscription_id}/resourceGroups/${AZURE_RESOURCE_GROUP_NAME}/providers/Microsoft.Search/searchServices/${AZURE_SEARCH_SERVICE_NAME}"
    echo "  🔐 Assigning Search roles to signed-in user..."
    az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" --role "Search Service Contributor" --scope "$search_scope" >/dev/null 2>&1 || true
    az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" --role "Search Index Data Contributor" --scope "$search_scope" >/dev/null 2>&1 || true
  fi

  export AZURE_SEARCH_ENDPOINT=$(echo $AZURE_SEARCH_ENDPOINT | tr -d '"')
  export AZURE_SEARCH_INDEX_NAME=$(echo $AZURE_SEARCH_INDEX_NAME | tr -d '"')
  export FOUNDRY_PROJECT_ENDPOINT=$(echo $FOUNDRY_PROJECT_ENDPOINT | tr -d '"')
  export EMBEDDING_MODEL_DEPLOYMENT_NAME=$(echo $EMBEDDING_MODEL_DEPLOYMENT_NAME | tr -d '"')

  echo "  📦 Ensuring python dependencies..."
  python -m pip install --disable-pip-version-check --quiet -r ./src/requirements.txt >/dev/null 2>&1 || true

  echo "  🚀 Running ingestion script..."
  python ./scripts/ingest_task_instructions.py
}

if ! try_run_ai_search; then
  echo "  ⚠️ AI Search provisioning/ingestion failed. Rerun manually: python ./scripts/ingest_task_instructions.py"
fi

# Generate test configuration
echo ""
echo "📝 Generating test configuration..."
./scripts/generate-test-config.sh

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "🎉 Post-provision setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "📝 Run integration tests:"
echo "   python tests/test_apim_mcp_connection.py --use-az-token"
echo ""

exit 0