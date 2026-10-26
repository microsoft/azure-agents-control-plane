<#
.SYNOPSIS
    Provisions a Fabric workspace, lakehouse, and warehouse using the Fabric REST API.
    Pipelines and semantic models must be created separately.

.DESCRIPTION
    This script automates the manual post-deployment steps for Fabric Data Agents:
    1. Creates a Fabric workspace (or uses an existing one)
    2. Assigns the workspace to the Fabric capacity
    3. Creates a Lakehouse item
    4. Creates a Warehouse item
    5. Outputs the resource IDs for use in azd env

    Requires:
    - Azure CLI logged in (`az login`)
    - Fabric capacity already provisioned via Bicep
    - Environment variables from `azd env get-values`

.PARAMETER WorkspaceName
    Name of the Fabric workspace to create. Default: "agents-<env>-ws"

.PARAMETER CapacityId
    Fabric capacity ID (from Bicep output). If omitted, reads from azd env.

.PARAMETER FabricEndpoint
    Fabric REST API base URL. Default: https://api.fabric.microsoft.com/v1

.PARAMETER Environment
    Named azd environment. Defaults to AZURE_ENV_NAME or the default azd environment.

.EXAMPLE
    ./deploy-fabric-workspace.ps1
    ./deploy-fabric-workspace.ps1 -WorkspaceName "my-fabric-ws"
#>

param(
    [string]$WorkspaceName = "",
    [string]$CapacityId = "",
    [ValidatePattern('^https://')]
    [string]$FabricEndpoint = "https://api.fabric.microsoft.com/v1",
    [string]$Environment = $env:AZURE_ENV_NAME
)

$ErrorActionPreference = "Stop"
$FabricEndpoint = $FabricEndpoint.TrimEnd('/')
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# ================================================================
# Helper: Get access token for Fabric API
# ================================================================
function Get-FabricToken {
    $token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) {
        throw "Failed to acquire Fabric API token. Run 'az login' first."
    }
    return $token
}

function Invoke-FabricApi {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null,
        [string]$Token,
        [switch]$ResultRequired
    )

    if (-not $Url.StartsWith("$FabricEndpoint/", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Fabric API returned a URL outside the configured endpoint."
    }
    $headers = @{
        "Authorization" = "Bearer $Token"
        "Content-Type"  = "application/json"
    }

    $params = @{
        Method  = $Method
        Uri     = $Url
        Headers = $headers
        TimeoutSec = 60
        MaximumRedirection = 0
    }

    if ($Body) {
        $params["Body"] = ($Body | ConvertTo-Json -Depth 10)
    }

    $response = Invoke-RestMethod @params -ResponseHeadersVariable responseHeaders -StatusCodeVariable statusCode -ErrorAction Stop
    if ($statusCode -ne 202) {
        return $response
    }

    $operationUrl = [string](@($responseHeaders['Location'])[0])
    if (-not $operationUrl -and $responseHeaders['x-ms-operation-id']) {
        $operationId = [string](@($responseHeaders['x-ms-operation-id'])[0])
        $operationUrl = "$FabricEndpoint/operations/$operationId"
    }
    if (-not $operationUrl.StartsWith("$FabricEndpoint/operations/", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Fabric accepted the request without a valid operation URL."
    }

    $deadline = [DateTime]::UtcNow.AddMinutes(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        $delay = 1
        if ($responseHeaders['Retry-After']) {
            $delay = [Math]::Clamp([int](@($responseHeaders['Retry-After'])[0]), 1, 60)
        }
        Start-Sleep -Seconds $delay
        $operation = Invoke-RestMethod -Method GET -Uri $operationUrl -Headers $headers `
            -TimeoutSec 60 -MaximumRedirection 0 -ResponseHeadersVariable responseHeaders -ErrorAction Stop
        if ($operation.status -eq 'Succeeded') {
            if ($ResultRequired) {
                return Invoke-RestMethod -Method GET -Uri "$operationUrl/result" -Headers $headers `
                    -TimeoutSec 60 -MaximumRedirection 0 -ErrorAction Stop
            }
            return $operation
        }
        if ($operation.status -in @('Failed', 'Cancelled')) {
            throw "Fabric operation $($operation.status): $($operation.error.message)"
        }
    }
    throw "Fabric operation timed out after 10 minutes."
}

function Get-FabricCollection {
    param([string]$Url, [string]$Token)

    $items = [System.Collections.Generic.List[object]]::new()
    do {
        $page = Invoke-FabricApi -Method GET -Url $Url -Token $Token
        foreach ($item in $page.value) { $items.Add($item) }
        $Url = $page.continuationUri
    } while ($Url)
    return $items.ToArray()
}

# ================================================================
# Load azd environment
# ================================================================
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  Fabric Data Agents - Workspace Provisioning" -ForegroundColor Cyan
Write-Host "============================================`n" -ForegroundColor Cyan

# Try to load azd env values
$envName = $Environment
if (-not $envName) {
    $environments = azd env list --output json
    if ($LASTEXITCODE -ne 0) { throw "Cannot list azd environments." }
    $envName = ($environments | ConvertFrom-Json | Where-Object { $_.IsDefault }).Name
}
if (-not $envName) { throw "Select an azd environment with -Environment." }
Write-Host "Loading azd environment: $envName" -ForegroundColor Gray
$envValues = azd env get-values -e $envName
if ($LASTEXITCODE -ne 0) { throw "Cannot load azd environment '$envName'." }
$envValues | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)="?(.*?)"?$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}

# Resolve parameters from azd env if not provided
if (-not $WorkspaceName) {
    $WorkspaceName = "agents-$envName-ws"
}

Write-Host "Workspace Name  : $WorkspaceName"
Write-Host "Fabric Endpoint : $FabricEndpoint"
Write-Host ""

# ================================================================
# Acquire token
# ================================================================
Write-Host "Acquiring Fabric API token..." -ForegroundColor Gray
$token = Get-FabricToken
Write-Host "[OK] Token acquired" -ForegroundColor Green

if (-not $CapacityId -and $env:FABRIC_CAPACITY_NAME) {
    $capacities = @(Get-FabricCollection -Url "$FabricEndpoint/capacities" -Token $token |
        Where-Object { $_.displayName -eq $env:FABRIC_CAPACITY_NAME })
    if ($capacities.Count -ne 1) {
        throw "Expected one visible Fabric capacity named '$($env:FABRIC_CAPACITY_NAME)'. Supply -CapacityId."
    }
    $CapacityId = $capacities[0].id
}

# ================================================================
# Step 1: Create or find workspace
# ================================================================
Write-Host "`n--- Step 1: Workspace ---" -ForegroundColor Yellow

$workspaces = Get-FabricCollection -Url "$FabricEndpoint/workspaces" -Token $token
$matchingWorkspaces = @($workspaces | Where-Object { $_.displayName -eq $WorkspaceName })
if ($matchingWorkspaces.Count -gt 1) { throw "Multiple workspaces are named '$WorkspaceName'. Use a unique name." }
$existing = $matchingWorkspaces | Select-Object -First 1

if ($existing) {
    $workspaceId = $existing.id
    if (-not $CapacityId -and -not $existing.capacityId) {
        throw "The existing workspace needs a Fabric capacity. Supply -CapacityId."
    }
    Write-Host "[OK] Workspace already exists: $workspaceId" -ForegroundColor Green
}
else {
    if (-not $CapacityId) { throw "Supply -CapacityId or configure FABRIC_CAPACITY_NAME before creating a workspace." }
    Write-Host "Creating workspace '$WorkspaceName'..."
    $wsBody = @{
        displayName = $WorkspaceName
        description = "Fabric Data Agents workspace for Azure Agents Control Plane"
    }

    # If capacity ID is known, assign it
    if ($CapacityId) {
        $wsBody["capacityId"] = $CapacityId
    }

    $wsResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces" -Body $wsBody -Token $token -ResultRequired
    $workspaceId = $wsResult.id
    if (-not $workspaceId) { throw "Workspace creation did not return an ID." }
    Write-Host "[OK] Workspace created: $workspaceId" -ForegroundColor Green
}

# Assign to capacity if provided and not already assigned
if ($CapacityId -and $existing -and $existing.capacityId -ne $CapacityId) {
    Write-Host "Assigning workspace to capacity $CapacityId..."
    $null = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/assignToCapacity" -Body @{ capacityId = $CapacityId } -Token $token
    Write-Host "[OK] Capacity assigned" -ForegroundColor Green
}

# ================================================================
# Step 2: Create Lakehouse
# ================================================================
Write-Host "`n--- Step 2: Lakehouse ---" -ForegroundColor Yellow

$lakehouseName = "AgentsLakehouse"
$items = Get-FabricCollection -Url "$FabricEndpoint/workspaces/$workspaceId/lakehouses" -Token $token
$existingLh = $items | Where-Object { $_.displayName -eq $lakehouseName }

if ($existingLh) {
    $lakehouseId = $existingLh.id
    Write-Host "[OK] Lakehouse already exists: $lakehouseId" -ForegroundColor Green
}
else {
    Write-Host "Creating lakehouse '$lakehouseName'..."
    $lhBody = @{
        displayName = $lakehouseName
        description = "Lakehouse for AI agent data queries - customer analytics, telemetry, and domain data"
        type        = "Lakehouse"
    }
    $lhResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/items" -Body $lhBody -Token $token -ResultRequired
    $lakehouseId = $lhResult.id
    if (-not $lakehouseId) { throw "Lakehouse creation did not return an ID." }
    Write-Host "[OK] Lakehouse created: $lakehouseId" -ForegroundColor Green
}

# ================================================================
# Step 3: Create Warehouse
# ================================================================
Write-Host "`n--- Step 3: Warehouse ---" -ForegroundColor Yellow

$warehouseName = "AgentsWarehouse"
$whItems = Get-FabricCollection -Url "$FabricEndpoint/workspaces/$workspaceId/warehouses" -Token $token
$existingWh = $whItems | Where-Object { $_.displayName -eq $warehouseName }

if ($existingWh) {
    $warehouseId = $existingWh.id
    Write-Host "[OK] Warehouse already exists: $warehouseId" -ForegroundColor Green
}
else {
    Write-Host "Creating warehouse '$warehouseName'..."
    $whBody = @{
        displayName = $warehouseName
        description = "Data warehouse for structured analytics - sales, reporting, aggregations"
        type        = "Warehouse"
    }
    $whResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/items" -Body $whBody -Token $token -ResultRequired
    $warehouseId = $whResult.id
    if (-not $warehouseId) { throw "Warehouse creation did not return an ID." }
    Write-Host "[OK] Warehouse created: $warehouseId" -ForegroundColor Green
}

# ================================================================
# Step 4: Output and save to azd env
# ================================================================
# Save to azd env
Write-Host "Saving to azd environment '$envName'..." -ForegroundColor Gray
$resourceValues = [ordered]@{
    FABRIC_WORKSPACE_ID = $workspaceId
    FABRIC_LAKEHOUSE_ID = $lakehouseId
    FABRIC_WAREHOUSE_ID = $warehouseId
    FABRIC_LAKEHOUSE_NAME = $lakehouseName
}
foreach ($entry in $resourceValues.GetEnumerator()) {
    if (-not $entry.Value) { throw "Missing resource ID or name for $($entry.Key)." }
    azd env set $entry.Key $entry.Value -e $envName
    if ($LASTEXITCODE -ne 0) { throw "Failed to save $($entry.Key) to azd environment '$envName'." }
}
Write-Host "[OK] Environment variables saved" -ForegroundColor Green

Write-Host "`nProvisioning Complete!" -ForegroundColor Green
foreach ($entry in $resourceValues.GetEnumerator()) { Write-Host "  $($entry.Key) = $($entry.Value)" }

Write-Host "`nNext steps:" -ForegroundColor Yellow
Write-Host "  1. Follow docs/FABRIC_DATA_AGENTS_SETUP_GUIDE.md for explicit runtime opt-in"
Write-Host "  2. (Optional) Create a Data Pipeline in the Fabric portal"
Write-Host "  3. (Optional) Create a Semantic Model / Power BI dataset"
Write-Host "  4. Grant the runtime managed identity the required Fabric workspace access"
Write-Host ""
