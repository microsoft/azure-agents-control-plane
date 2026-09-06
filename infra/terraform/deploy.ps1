#!/usr/bin/env pwsh

[CmdletBinding()]
param(
    [ValidateSet("validate", "plan", "apply", "destroy")]
    [string]$Action = "apply",

    [string]$EnvironmentName = "",
    [string]$Location = "",
    [string]$VnetEnabled = "",
    [string]$VarFile = "",
    [switch]$AutoApprove,
    [switch]$SkipPostProvision
)

$ErrorActionPreference = "Stop"
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$terraformDirectory = $PSScriptRoot
$infraDirectory = Split-Path -Parent $terraformDirectory
$repositoryRoot = Split-Path -Parent $infraDirectory
$generatedDirectory = Join-Path $terraformDirectory "generated"
$generatedTemplate = Join-Path $generatedDirectory "main.json"

foreach ($command in @("az", "terraform")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command '$command' was not found."
    }
}

function Get-AzdValue {
    param([Parameter(Mandatory)][string]$Name)

    $value = & azd env get-value $Name 2>$null
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return "$value".Trim().Trim('"')
}

New-Item -ItemType Directory -Path $generatedDirectory -Force | Out-Null
& az bicep build --file (Join-Path $infraDirectory "main.bicep") --outfile $generatedTemplate
if ($LASTEXITCODE -ne 0) {
    throw "Bicep compilation failed."
}

& terraform "-chdir=$terraformDirectory" init
if ($LASTEXITCODE -ne 0) {
    throw "terraform init failed."
}

if ($Action -eq "validate") {
    & terraform "-chdir=$terraformDirectory" validate
    if ($LASTEXITCODE -ne 0) {
        throw "terraform validate failed."
    }
    exit 0
}

if (-not (Get-Command azd -ErrorAction SilentlyContinue)) {
    throw "Required command 'azd' was not found."
}

if (-not $EnvironmentName) {
    $EnvironmentName = if ($env:AZURE_ENV_NAME) { $env:AZURE_ENV_NAME } else { Get-AzdValue "AZURE_ENV_NAME" }
}
if (-not $Location) {
    $Location = if ($env:AZURE_LOCATION) { $env:AZURE_LOCATION } else { Get-AzdValue "AZURE_LOCATION" }
}
if (-not $VnetEnabled) {
    $VnetEnabled = Get-AzdValue "VNET_ENABLED"
}
if (-not $VnetEnabled) {
    $VnetEnabled = "true"
}

if (-not $EnvironmentName -or -not $Location) {
    throw "EnvironmentName and Location are required. Pass them explicitly or select an azd environment that defines AZURE_ENV_NAME and AZURE_LOCATION."
}
if ($VnetEnabled.ToLowerInvariant() -notin @("true", "false")) {
    throw "VnetEnabled must be 'true' or 'false'."
}

$subscriptionId = $env:ARM_SUBSCRIPTION_ID
if (-not $subscriptionId) {
    $subscriptionId = (& az account show --query id --output tsv).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to determine the Azure subscription. Run 'az login' or set ARM_SUBSCRIPTION_ID."
    }
}
if (-not $subscriptionId) {
    throw "Unable to determine the Azure subscription. Run 'az login' or set ARM_SUBSCRIPTION_ID."
}

$terraformArguments = @(
    "-var=subscription_id=$subscriptionId",
    "-var=environment_name=$EnvironmentName",
    "-var=location=$Location",
    "-var=vnet_enabled=$($VnetEnabled.ToLowerInvariant())",
    "-var=bicep_template_path=generated/main.json"
)

$azdVariableMappings = [ordered]@{
    FABRIC_ENABLED                 = "fabric_enabled"
    FABRIC_SKU                     = "fabric_sku_name"
    FABRIC_ADMIN_EMAIL             = "fabric_admin_email"
    FABRIC_PRIVATE_LINK_SERVICE_ID = "fabric_private_link_service_id"
    AGENT_IDENTITY_ENABLED         = "agent_identity_enabled"
    DEVELOPER_PRINCIPAL_ID         = "developer_principal_id"
    DEVELOPER_IP_ADDRESS           = "developer_ip_address"
    FABRIC_DATA_AGENTS_ENABLED     = "fabric_data_agents_enabled"
    FABRIC_WORKSPACE_ID            = "fabric_workspace_id"
    DEFENDER_ENABLED               = "defender_enabled"
    DEFENDER_SECURITY_CONTACT_EMAIL = "defender_security_contact_email"
    DEFENDER_SECURITY_CONTACT_PHONE = "defender_security_contact_phone"
    PURVIEW_ENABLED                = "purview_enabled"
    SEARCH_ENABLED                 = "search_enabled"
}
foreach ($mapping in $azdVariableMappings.GetEnumerator()) {
    $value = Get-AzdValue $mapping.Key
    if ($value) {
        $terraformArguments += "-var=$($mapping.Value)=$value"
    }
}

if ($VarFile) {
    $terraformArguments += "-var-file=$VarFile"
}
if ($AutoApprove -and $Action -in @("apply", "destroy")) {
    $terraformArguments += "-auto-approve"
}

& terraform "-chdir=$terraformDirectory" $Action @terraformArguments
if ($LASTEXITCODE -ne 0) {
    throw "terraform $Action failed."
}

if ($Action -ne "apply") {
    exit 0
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw "Required command 'python' or 'python3' was not found."
}

$environmentJson = & terraform "-chdir=$terraformDirectory" output -json azd_environment
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Terraform deployment outputs."
}
$environmentJson | & $pythonCommand.Source (Join-Path $terraformDirectory "export_azd_env.py")
if ($LASTEXITCODE -ne 0) {
    throw "Unable to export Terraform outputs to azd."
}

if (-not $SkipPostProvision) {
    Push-Location $repositoryRoot
    try {
        & (Join-Path $infraDirectory "hooks/postprovision.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "The post-provision hook failed."
        }
    } finally {
        Pop-Location
    }
}