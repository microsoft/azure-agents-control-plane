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
$runtime = if ($env:MCP_AGENT_RUNTIME) { $env:MCP_AGENT_RUNTIME.Trim().ToLowerInvariant() } else { "python" }

switch ($runtime) {
    "python" {
        $dockerfile = "Dockerfile"
        $defaultImageTag = "latest"
    }
    "typescript" {
        $dockerfile = "Dockerfile.typescript"
        $defaultImageTag = "typescript"
    }
    default {
        Write-Host "Unsupported MCP_AGENT_RUNTIME '$runtime'. Use 'python' or 'typescript'." -ForegroundColor Red
        exit 1
    }
}
$IMAGE_TAG = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { $defaultImageTag }
$FULL_IMAGE_NAME = "$($env:CONTAINER_REGISTRY)/$($IMAGE_NAME):$($IMAGE_TAG)"

Write-Host "Building $runtime image via ACR Tasks (linux/amd64): $FULL_IMAGE_NAME" -ForegroundColor Cyan
Write-Host "Dockerfile: src/$dockerfile" -ForegroundColor Cyan

$registryName = $env:CONTAINER_REGISTRY -replace '\..*', ''
$originalPublicNetworkAccess = (az acr show --name $registryName --query publicNetworkAccess --output tsv).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to read ACR network configuration" -ForegroundColor Red
    exit $LASTEXITCODE
}
$originalDefaultAction = (az acr show --name $registryName --query networkRuleSet.defaultAction --output tsv).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to read ACR firewall configuration" -ForegroundColor Red
    exit $LASTEXITCODE
}
if (-not $originalDefaultAction) {
    $originalDefaultAction = "Allow"
}
$restorePrivateAccess = $originalPublicNetworkAccess -eq "Disabled"
$acrBuildExit = 1
$restoreExit = 0

# Build and push using ACR Tasks: cloud build, always linux/amd64,
# with no dependency on the local Docker daemon or host CPU architecture.
try {
    if ($restorePrivateAccess) {
        Write-Host "Temporarily enabling authenticated public access for the ACR Task build" -ForegroundColor Cyan
        az acr update --name $registryName --public-network-enabled true --default-action Allow --output none
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to enable ACR access for the remote build"
        }
    }

    Push-Location src
    try {
        az acr build --registry $registryName --image "$($IMAGE_NAME):$($IMAGE_TAG)" --platform linux/amd64 --file $dockerfile .
        $acrBuildExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    if ($restorePrivateAccess) {
        Write-Host "Restoring private ACR network access" -ForegroundColor Cyan
        az acr update --name $registryName --public-network-enabled false --default-action $originalDefaultAction --output none
        $restoreExit = $LASTEXITCODE
    }
}

if ($acrBuildExit -ne 0) {
    Write-Host "ACR build failed (exit $acrBuildExit)" -ForegroundColor Red
    exit $acrBuildExit
}
if ($restoreExit -ne 0) {
    Write-Host "Image was pushed, but the ACR network policy could not be restored" -ForegroundColor Red
    exit $restoreExit
}

Write-Host "Image built and pushed successfully: $FULL_IMAGE_NAME" -ForegroundColor Green
