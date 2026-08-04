# Build and push MCP server Docker image to Azure Container Registry
# PowerShell version

$ErrorActionPreference = "Stop"

# Check required environment variables
if (-not $env:CONTAINER_REGISTRY) {
    Write-Host "❌ CONTAINER_REGISTRY environment variable is not set" -ForegroundColor Red
    exit 1
}

# Set defaults
$IMAGE_NAME = if ($env:IMAGE_NAME) { $env:IMAGE_NAME } else { "mcp-agents" }
$IMAGE_TAG = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { "latest" }
$FULL_IMAGE_NAME = "$($env:CONTAINER_REGISTRY)/$($IMAGE_NAME):$($IMAGE_TAG)"

Write-Host "Building image via ACR Tasks (linux/amd64): $FULL_IMAGE_NAME" -ForegroundColor Cyan

$registryName = $env:CONTAINER_REGISTRY -replace '\..*', ''

# Build and push using ACR Tasks: cloud build, always linux/amd64,
# with no dependency on the local Docker daemon or host CPU architecture.
Push-Location src
az acr build --registry $registryName --image "$($IMAGE_NAME):$($IMAGE_TAG)" --platform linux/amd64 .
$acrBuildExit = $LASTEXITCODE
Pop-Location

if ($acrBuildExit -ne 0) {
    Write-Host "ACR build failed (exit $acrBuildExit)" -ForegroundColor Red
    exit $acrBuildExit
}

Write-Host "Image built and pushed successfully: $FULL_IMAGE_NAME" -ForegroundColor Green
