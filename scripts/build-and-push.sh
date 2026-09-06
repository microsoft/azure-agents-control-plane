#!/bin/bash
# Build and push MCP server Docker image to Azure Container Registry

set -e

# Check required environment variables
if [ -z "$CONTAINER_REGISTRY" ]; then
    echo "❌ CONTAINER_REGISTRY environment variable is not set"
    exit 1
fi

# Set defaults
IMAGE_NAME="${IMAGE_NAME:-mcp-agents}"
MCP_AGENT_RUNTIME="${MCP_AGENT_RUNTIME:-python}"

case "${MCP_AGENT_RUNTIME}" in
    python)
        DOCKERFILE="Dockerfile"
        DEFAULT_IMAGE_TAG="latest"
        ;;
    typescript)
        DOCKERFILE="Dockerfile.typescript"
        DEFAULT_IMAGE_TAG="typescript"
        ;;
    *)
        echo "Unsupported MCP_AGENT_RUNTIME '${MCP_AGENT_RUNTIME}'. Use 'python' or 'typescript'."
        exit 1
        ;;
esac
IMAGE_TAG="${IMAGE_TAG:-${DEFAULT_IMAGE_TAG}}"
FULL_IMAGE_NAME="${CONTAINER_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
REGISTRY_NAME="${CONTAINER_REGISTRY%%.*}"

echo "🏗️  Building ${MCP_AGENT_RUNTIME} image via ACR Tasks (linux/amd64): ${FULL_IMAGE_NAME}"
echo "Dockerfile: src/${DOCKERFILE}"

ORIGINAL_PUBLIC_NETWORK_ACCESS=$(az acr show --name "${REGISTRY_NAME}" --query publicNetworkAccess --output tsv)
ORIGINAL_DEFAULT_ACTION=$(az acr show --name "${REGISTRY_NAME}" --query networkRuleSet.defaultAction --output tsv)
ORIGINAL_DEFAULT_ACTION="${ORIGINAL_DEFAULT_ACTION:-Allow}"
RESTORE_PRIVATE_ACCESS=false

restore_registry_access() {
    if [ "${RESTORE_PRIVATE_ACCESS}" = "true" ]; then
        echo "Restoring private ACR network access"
        az acr update \
            --name "${REGISTRY_NAME}" \
            --public-network-enabled false \
            --default-action "${ORIGINAL_DEFAULT_ACTION}" \
            --output none
    fi
}

trap restore_registry_access EXIT

if [ "${ORIGINAL_PUBLIC_NETWORK_ACCESS}" = "Disabled" ]; then
    RESTORE_PRIVATE_ACCESS=true
    echo "Temporarily enabling authenticated public access for the ACR Task build"
    az acr update \
        --name "${REGISTRY_NAME}" \
        --public-network-enabled true \
        --default-action Allow \
        --output none
fi

cd src
az acr build \
    --registry "${REGISTRY_NAME}" \
    --image "${IMAGE_NAME}:${IMAGE_TAG}" \
    --platform linux/amd64 \
    --file "${DOCKERFILE}" \
    .

echo "✅ Image built and pushed successfully: ${FULL_IMAGE_NAME}"
