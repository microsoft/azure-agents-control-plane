#!/usr/bin/env pwsh

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
  [string]$Environment,

  [ValidatePattern('^[^@\s]+@[^@\s]+$')]
  [string]$OwnerUpn = 'christava@microsoft.com',

  [ValidateLength(1, 128)]
  [string]$AgentName = 'Next Best Action',

  [ValidatePattern('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')]
  [string]$ClientAppId,

  [ValidateScript({ $_ -match '^[A-Za-z0-9_]+$' })]
  [string[]]$EligibleLicenseSkuPartNumber = @('Microsoft_Agent_365_Tier3', 'MICROSOFT_365_E7'),

  [switch]$Apply,
  [switch]$Deploy
)

$ErrorActionPreference = 'Stop'
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}
if ($Deploy -and -not $Apply) {
  throw '-Deploy requires -Apply.'
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = if (Test-Path (Join-Path $root '.venv/Scripts/python.exe')) {
  (Resolve-Path (Join-Path $root '.venv/Scripts/python.exe')).Path
} else {
  (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}
$a365 = (Get-Command a365 -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$azd = (Get-Command azd -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$az = (Get-Command az -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$Failure
  )
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) { throw $Failure }
}

Push-Location $root
try {
  $versionText = (& $a365 --version | Out-String).Trim()
  if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '(\d+\.\d+\.\d+)') {
    throw 'Cannot determine the Agent 365 CLI version.'
  }
  if ([version]$matches[1] -lt [version]'1.1.221') {
    throw 'Agent 365 CLI 1.1.221 or newer is required.'
  }

  $environmentValues = & $azd env get-values --environment $Environment --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or -not $environmentValues) {
    throw "Cannot load azd environment '$Environment'."
  }
  $tenantId = [string]$environmentValues.AZURE_TENANT_ID
  if ($tenantId -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw 'The azd environment has no valid AZURE_TENANT_ID.'
  }
  $subscriptionId = [string]$environmentValues.AZURE_SUBSCRIPTION_ID
  $resourceGroupName = [string]$environmentValues.AZURE_RESOURCE_GROUP_NAME
  $uamiClientId = [string]$environmentValues.MCP_SERVER_IDENTITY_CLIENT_ID
  foreach ($id in @($subscriptionId, $uamiClientId)) {
    if ($id -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
      throw 'The azd environment has invalid subscription or UAMI configuration.'
    }
  }
  if (-not $resourceGroupName) { throw 'The azd environment has no resource group.' }

  $account = & $az account show --output json --only-show-errors | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or $account.tenantId -ne $tenantId -or $account.state -ne 'Enabled') {
    throw 'Azure CLI is not signed into the enabled target tenant.'
  }
  $owner = & $az ad signed-in-user show --output json --only-show-errors | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or -not $owner.id) {
    throw 'The signed-in Azure CLI principal is not a user.'
  }
  $ownerNames = @([string]$owner.userPrincipalName, [string]$owner.mail) | ForEach-Object { $_.Trim().ToLowerInvariant() }
  if ($ownerNames -notcontains $OwnerUpn.Trim().ToLowerInvariant()) {
    throw "Sign in as '$OwnerUpn'; the setup caller becomes the blueprint, Agent ID, and registration owner."
  }

  $identities = & $az identity list --subscription $subscriptionId --resource-group $resourceGroupName --output json --only-show-errors | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Cannot read the target managed identity.' }
  $uami = @($identities | Where-Object { $_.clientId -eq $uamiClientId })
  $uamiPrincipalId = if ($uami.Count -eq 1) { [string]($uami[0].principalId) } else { '' }
  if ($uamiPrincipalId -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw 'Cannot uniquely resolve the configured AKS UAMI.'
  }

  if (-not $Apply) {
    $previewDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("nba-agent365-preview-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $previewDirectory -ErrorAction Stop | Out-Null
    try {
      $previewClientAppId = if ($ClientAppId) { $ClientAppId } else { 'f54280f4-395e-4ea8-9e48-bf2d4952aa14' }
      @{
        tenantId = $tenantId
        clientAppId = $previewClientAppId
        authMode = 's2s'
        agentIdentityDisplayName = "$AgentName Agent"
        agentBlueprintDisplayName = "$AgentName Blueprint"
        agentDescription = $AgentName
        aiTeammate = $false
        useBlueprint = $true
      } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $previewDirectory 'a365.config.json') -Encoding utf8
      @{
        managedIdentityPrincipalId = $uamiPrincipalId
      } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $previewDirectory 'a365.generated.config.json') -Encoding utf8
      Write-Host "Repository target: reuse AKS UAMI '$($uami[0].name)' ($uamiPrincipalId)."
      Write-Host "The CLI dry-run may describe its generic managed-identity step as 'create'; apply mode seeds this existing principal."
      Push-Location $previewDirectory
      try {
        Invoke-Checked -FilePath $a365 -Arguments @('setup', 'all', '--dry-run') `
          -Failure 'Agent 365 setup preview failed.'
      } finally {
        Pop-Location
      }
    } finally {
      Remove-Item -LiteralPath $previewDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host 'Preview complete. Re-run with -Apply after assigning the documented tenant roles and an eligible observability license.'
    return
  }

  Invoke-Checked -FilePath $a365 -Arguments @('setup', 'requirements') `
    -Failure 'Agent 365 prerequisite validation did not pass.'

  $prepare = @(
    'scripts/adopt_agent365_onboarding.py', '--environment', $Environment,
    '--owner-upn', $OwnerUpn, '--agent-name', $AgentName,
    '--config-dir', $root, '--prepare'
  )
  if ($ClientAppId) { $prepare += @('--client-app-id', $ClientAppId) }
  foreach ($sku in $EligibleLicenseSkuPartNumber) { $prepare += @('--eligible-license-sku', $sku) }
  Invoke-Checked -FilePath $python -Arguments $prepare `
    -Failure 'Agent 365 UAMI-backed setup configuration could not be prepared.'

  Invoke-Checked -FilePath $a365 -Arguments @('setup', 'all', '--dry-run') `
    -Failure 'Agent 365 setup preview failed after configuration was prepared.'
  Invoke-Checked -FilePath $a365 -Arguments @('setup', 'all') `
    -Failure 'Agent 365 setup did not complete.'

  $adopt = @(
    'scripts/adopt_agent365_onboarding.py', '--environment', $Environment,
    '--owner-upn', $OwnerUpn, '--config-dir', $root, '--apply'
  )
  foreach ($sku in $EligibleLicenseSkuPartNumber) { $adopt += @('--eligible-license-sku', $sku) }
  Invoke-Checked -FilePath $python -Arguments $adopt `
    -Failure 'Agent 365 identity, ownership, registration, federation, licensing, or observability verification failed.'

  Invoke-Checked -FilePath $python -Arguments @(
    'scripts/deployment_gate.py', '--from-azd', '--environment', $Environment, '--check'
  ) -Failure 'The Agent 365 deployment gate did not pass.'

  if ($Deploy) {
    Invoke-Checked -FilePath $azd -Arguments @('provision', '--environment', $Environment) `
      -Failure 'Infrastructure deployment or the gated AKS rollout failed.'
  }

  Write-Host 'Agent 365 onboarding and repository adoption completed.'
  if (-not $Deploy) {
    Write-Host "Run './scripts/install-agent365.ps1 -Environment $Environment -Apply -Deploy' to perform the gated build and rollout."
  }
} finally {
  Pop-Location
}