#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Generate test configuration from Azure deployment outputs
.DESCRIPTION
    Extracts outputs from the Azure Bicep deployment and generates configuration
    files for test scripts (both .env and .json formats)
.PARAMETER DeploymentName
    Name of the Azure deployment (defaults to 'main')
.PARAMETER ResourceGroup
    Resource group name (auto-detected if not provided)
.PARAMETER OutputDir
    Directory to write configuration files (defaults to tests/)
#>

param(
    [string]$DeploymentName = "main",
    [string]$ResourceGroup,
    [string]$OutputDir = "tests"
)

$ErrorActionPreference = "Stop"

Write-Host "`n╔══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║       Generating Test Configuration from Deployment     ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════════╝`n" -ForegroundColor Cyan

# Get current subscription
$subscriptionInfo = az account show | ConvertFrom-Json
$subscriptionId = $subscriptionInfo.id
$tenantId = $subscriptionInfo.tenantId

Write-Host "📍 Subscription: $($subscriptionInfo.name)" -ForegroundColor Yellow
Write-Host "📍 Tenant: $tenantId`n" -ForegroundColor Yellow

function ConvertFrom-AzdValue([object]$Value) {
    if ($null -eq $Value) {
        return ""
    }
    return ([string]$Value).Trim('"')
}

# Prefer the current azd environment because it contains the outputs from the
# exact deployment that invoked this post-provision hook.
$azdEnvValues = $null
try {
    $azdEnvOutput = azd env get-values 2>$null
    if ($LASTEXITCODE -eq 0 -and $azdEnvOutput) {
        $azdEnvValues = $azdEnvOutput | ConvertFrom-StringData
    }
} catch {
    $azdEnvValues = $null
}

if ($azdEnvValues -and $azdEnvValues.MCP_BASE_URL) {
    Write-Host "🔍 Using outputs from the active azd environment" -ForegroundColor Cyan
    $DeploymentName = ConvertFrom-AzdValue $azdEnvValues.AZURE_ENV_NAME
    $config = @{
        APIM_BASE_URL = ConvertFrom-AzdValue $azdEnvValues.MCP_BASE_URL
        APIM_GATEWAY_URL = ConvertFrom-AzdValue $azdEnvValues.APIM_GATEWAY_URL
        APIM_OAUTH_AUTHORIZE_URL = ConvertFrom-AzdValue $azdEnvValues.MCP_OAUTH_AUTHORIZE_URL
        APIM_OAUTH_TOKEN_URL = ConvertFrom-AzdValue $azdEnvValues.MCP_OAUTH_TOKEN_URL
        MCP_CLIENT_ID = ConvertFrom-AzdValue $azdEnvValues.MCP_CLIENT_ID
        AZURE_TENANT_ID = ConvertFrom-AzdValue $azdEnvValues.AZURE_TENANT_ID
        AZURE_SUBSCRIPTION_ID = ConvertFrom-AzdValue $azdEnvValues.AZURE_SUBSCRIPTION_ID
        AZURE_RESOURCE_GROUP_NAME = ConvertFrom-AzdValue $azdEnvValues.AZURE_RESOURCE_GROUP_NAME
        AZURE_LOCATION = ConvertFrom-AzdValue $azdEnvValues.AZURE_LOCATION
        AKS_CLUSTER_NAME = ConvertFrom-AzdValue $azdEnvValues.AKS_CLUSTER_NAME
        CONTAINER_REGISTRY = ConvertFrom-AzdValue $azdEnvValues.CONTAINER_REGISTRY
    }
} else {
    # Fall back to a subscription deployment lookup when no azd environment is active.
    if ($DeploymentName -eq "main") {
        Write-Host "🔍 Finding most recent deployment..." -ForegroundColor Cyan
        $recentDeployment = az deployment sub list --query "[?starts_with(name, 'apim-mcp')] | [0].name" -o tsv
        if ($recentDeployment) {
            $DeploymentName = $recentDeployment
            Write-Host "✅ Found deployment: $DeploymentName" -ForegroundColor Green
        } else {
            Write-Host "⚠️  No recent deployment found, checking 'main'..." -ForegroundColor Yellow
        }
    }

    Write-Host "`n📤 Retrieving deployment outputs..." -ForegroundColor Cyan
    try {
        $outputsJson = az deployment sub show --name $DeploymentName --query "properties.outputs" -o json
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to get deployment outputs"
        }
        $outputs = $outputsJson | ConvertFrom-Json
    } catch {
        Write-Host "❌ Error: Could not retrieve deployment outputs" -ForegroundColor Red
        Write-Host "   Make sure the deployment exists and completed successfully" -ForegroundColor Red
        Write-Host "   Deployment name: $DeploymentName" -ForegroundColor Yellow
        exit 1
    }

    $config = @{
        APIM_BASE_URL = $outputs.MCP_BASE_URL.value
        APIM_GATEWAY_URL = $outputs.APIM_GATEWAY_URL.value
        APIM_OAUTH_AUTHORIZE_URL = $outputs.MCP_OAUTH_AUTHORIZE_URL.value
        APIM_OAUTH_TOKEN_URL = $outputs.MCP_OAUTH_TOKEN_URL.value
        MCP_CLIENT_ID = $outputs.MCP_CLIENT_ID.value
        AZURE_TENANT_ID = $outputs.AZURE_TENANT_ID.value
        AZURE_SUBSCRIPTION_ID = $outputs.AZURE_SUBSCRIPTION_ID.value
        AZURE_RESOURCE_GROUP_NAME = $outputs.AZURE_RESOURCE_GROUP_NAME.value
        AZURE_LOCATION = $outputs.AZURE_LOCATION.value
        AKS_CLUSTER_NAME = $outputs.AKS_CLUSTER_NAME.value
        CONTAINER_REGISTRY = $outputs.CONTAINER_REGISTRY.value
    }
}

# Add redirect URI (standard for OAuth testing)
$config.REDIRECT_URI = "http://localhost:8080/callback"

Write-Host "✅ Configuration extracted:" -ForegroundColor Green
foreach ($key in $config.Keys | Sort-Object) {
    $value = $config[$key]
    if ($value) {
        $displayValue = if ($value.Length -gt 60) { $value.Substring(0, 60) + "..." } else { $value }
        Write-Host "   $key = $displayValue" -ForegroundColor White
    } else {
        Write-Host "   $key = <not set>" -ForegroundColor Gray
    }
}

# Ensure output directory exists
$outputPath = Join-Path (Join-Path $PSScriptRoot "..") $OutputDir
if (-not (Test-Path $outputPath)) {
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
}

# Generate .env file
$envFile = Join-Path $outputPath "mcp_test.env"
Write-Host "`n📝 Writing .env file: $envFile" -ForegroundColor Cyan

$envContent = @"
# MCP Test Configuration
# Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
# Deployment: $DeploymentName

# APIM Endpoints
APIM_BASE_URL=$($config.APIM_BASE_URL)
APIM_GATEWAY_URL=$($config.APIM_GATEWAY_URL)
APIM_OAUTH_AUTHORIZE_URL=$($config.APIM_OAUTH_AUTHORIZE_URL)
APIM_OAUTH_TOKEN_URL=$($config.APIM_OAUTH_TOKEN_URL)

# OAuth Configuration
MCP_CLIENT_ID=$($config.MCP_CLIENT_ID)
REDIRECT_URI=$($config.REDIRECT_URI)

# Azure Resources
AZURE_TENANT_ID=$($config.AZURE_TENANT_ID)
AZURE_SUBSCRIPTION_ID=$($config.AZURE_SUBSCRIPTION_ID)
AZURE_RESOURCE_GROUP_NAME=$($config.AZURE_RESOURCE_GROUP_NAME)
AZURE_LOCATION=$($config.AZURE_LOCATION)
AKS_CLUSTER_NAME=$($config.AKS_CLUSTER_NAME)
CONTAINER_REGISTRY=$($config.CONTAINER_REGISTRY)
"@

[System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.UTF8Encoding]::new($false))
Write-Host "✅ .env file created: $envFile" -ForegroundColor Green

# Generate .json file
$jsonFile = Join-Path $outputPath "mcp_test_config.json"
Write-Host "📝 Writing JSON file: $jsonFile" -ForegroundColor Cyan

$jsonConfig = @{
    generated_at = Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ"
    deployment_name = $DeploymentName
    apim = @{
        base_url = $config.APIM_BASE_URL
        gateway_url = $config.APIM_GATEWAY_URL
        oauth_authorize_url = $config.APIM_OAUTH_AUTHORIZE_URL
        oauth_token_url = $config.APIM_OAUTH_TOKEN_URL
    }
    oauth = @{
        client_id = $config.MCP_CLIENT_ID
        redirect_uri = $config.REDIRECT_URI
        scope = "openid profile email"
    }
    azure = @{
        tenant_id = $config.AZURE_TENANT_ID
        subscription_id = $config.AZURE_SUBSCRIPTION_ID
        resource_group = $config.AZURE_RESOURCE_GROUP_NAME
        location = $config.AZURE_LOCATION
    }
    resources = @{
        aks_cluster_name = $config.AKS_CLUSTER_NAME
        container_registry = $config.CONTAINER_REGISTRY
    }
}

$jsonContent = $jsonConfig | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($jsonFile, $jsonContent, [System.Text.UTF8Encoding]::new($false))
Write-Host "✅ JSON file created: $jsonFile" -ForegroundColor Green

Write-Host "`n🎉 Configuration files generated successfully!" -ForegroundColor Green
Write-Host "`n📋 Next steps:" -ForegroundColor Cyan
Write-Host "   1. Review the generated configuration files" -ForegroundColor White
Write-Host "   2. Run test with: python tests/test_mcp_fixed_session.py" -ForegroundColor White
Write-Host "   3. Generate OAuth token with: --generate-token flag`n" -ForegroundColor White
