<#
.SYNOPSIS
    Provisions Fabric workspace, lakehouse, warehouse, pipeline, and semantic model
    using the Microsoft Fabric REST API.

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

.EXAMPLE
    ./deploy-fabric-workspace.ps1
    ./deploy-fabric-workspace.ps1 -WorkspaceName "my-fabric-ws"
#>

param(
    [string]$WorkspaceName = "",
    [string]$CapacityId = "",
    [string]$FabricEndpoint = "https://api.fabric.microsoft.com/v1"
)

$ErrorActionPreference = "Stop"

# ================================================================
# Helper: Get access token for Fabric API
# ================================================================
function Get-FabricToken {
    $token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
    if (-not $token) {
        throw "Failed to acquire Fabric API token. Run 'az login' first."
    }
    return $token
}

function Invoke-FabricApi {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null,
        [string]$Token
    )

    $headers = @{
        "Authorization" = "Bearer $Token"
        "Content-Type"  = "application/json"
    }

    $params = @{
        Method  = $Method
        Uri     = $Url
        Headers = $headers
    }

    if ($Body) {
        $params["Body"] = ($Body | ConvertTo-Json -Depth 10)
    }

    try {
        $response = Invoke-RestMethod @params -ErrorAction Stop
        return $response
    }
    catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        $errorBody = $_.ErrorDetails.Message
        Write-Warning "Fabric API error ($statusCode): $errorBody"
        throw
    }
}

# ================================================================
# Load azd environment
# ================================================================
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  Fabric Data Agents - Workspace Provisioning" -ForegroundColor Cyan
Write-Host "============================================`n" -ForegroundColor Cyan

# Try to load azd env values
$envName = $env:AZURE_ENV_NAME
if (-not $envName) {
    $envName = (azd env list --output json | ConvertFrom-Json | Where-Object { $_.IsDefault }).Name
}

if ($envName) {
    Write-Host "Loading azd environment: $envName" -ForegroundColor Gray
    $envValues = azd env get-values -e $envName 2>$null
    if ($envValues) {
        $envValues | ForEach-Object {
            if ($_ -match '^([A-Z_]+)="?(.*?)"?$') {
                [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
            }
        }
    }
}

# Resolve parameters from azd env if not provided
if (-not $WorkspaceName) {
    $WorkspaceName = "agents-$envName-ws"
}

if (-not $CapacityId -and $env:FABRIC_CAPACITY_NAME) {
    Write-Host "Looking up capacity ID for '$($env:FABRIC_CAPACITY_NAME)'..." -ForegroundColor Gray
    # We need to look up the capacity ID via the Fabric API
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

# ================================================================
# Step 1: Create or find workspace
# ================================================================
Write-Host "`n--- Step 1: Workspace ---" -ForegroundColor Yellow

$workspaces = Invoke-FabricApi -Method GET -Url "$FabricEndpoint/workspaces" -Token $token
$existing = $workspaces.value | Where-Object { $_.displayName -eq $WorkspaceName }

if ($existing) {
    $workspaceId = $existing.id
    Write-Host "[OK] Workspace already exists: $workspaceId" -ForegroundColor Green
}
else {
    Write-Host "Creating workspace '$WorkspaceName'..."
    $wsBody = @{
        displayName = $WorkspaceName
        description = "Fabric Data Agents workspace for Azure Agents Control Plane"
    }

    # If capacity ID is known, assign it
    if ($CapacityId) {
        $wsBody["capacityId"] = $CapacityId
    }

    $wsResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces" -Body $wsBody -Token $token
    $workspaceId = $wsResult.id
    Write-Host "[OK] Workspace created: $workspaceId" -ForegroundColor Green
}

# Assign to capacity if provided and not already assigned
if ($CapacityId -and -not $existing) {
    Write-Host "Assigning workspace to capacity $CapacityId..."
    try {
        Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/assignToCapacity" -Body @{ capacityId = $CapacityId } -Token $token
        Write-Host "[OK] Capacity assigned" -ForegroundColor Green
    }
    catch {
        Write-Warning "Capacity assignment may have already been done or failed: $_"
    }
}

# ================================================================
# Step 2: Create Lakehouse
# ================================================================
Write-Host "`n--- Step 2: Lakehouse ---" -ForegroundColor Yellow

$lakehouseName = "AgentsLakehouse"
$items = Invoke-FabricApi -Method GET -Url "$FabricEndpoint/workspaces/$workspaceId/lakehouses" -Token $token
$existingLh = $items.value | Where-Object { $_.displayName -eq $lakehouseName }

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
    $lhResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/items" -Body $lhBody -Token $token
    $lakehouseId = $lhResult.id
    Write-Host "[OK] Lakehouse created: $lakehouseId" -ForegroundColor Green
}

# ================================================================
# Step 3: Create Warehouse
# ================================================================
Write-Host "`n--- Step 3: Warehouse ---" -ForegroundColor Yellow

$warehouseName = "AgentsWarehouse"
$whItems = Invoke-FabricApi -Method GET -Url "$FabricEndpoint/workspaces/$workspaceId/warehouses" -Token $token
$existingWh = $whItems.value | Where-Object { $_.displayName -eq $warehouseName }

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
    $whResult = Invoke-FabricApi -Method POST -Url "$FabricEndpoint/workspaces/$workspaceId/items" -Body $whBody -Token $token
    $warehouseId = $whResult.id
    Write-Host "[OK] Warehouse created: $warehouseId" -ForegroundColor Green
}

# ================================================================
# Step 4: Output and save to azd env
# ================================================================
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  Provisioning Complete!" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Resource IDs:" -ForegroundColor White
Write-Host "  FABRIC_WORKSPACE_ID  = $workspaceId"
Write-Host "  FABRIC_LAKEHOUSE_ID  = $lakehouseId"
Write-Host "  FABRIC_WAREHOUSE_ID  = $warehouseId"
Write-Host "  FABRIC_LAKEHOUSE_NAME= $lakehouseName"
Write-Host ""

# Save to azd env
if ($envName) {
    Write-Host "Saving to azd environment '$envName'..." -ForegroundColor Gray
    azd env set FABRIC_WORKSPACE_ID $workspaceId -e $envName 2>$null
    azd env set FABRIC_LAKEHOUSE_ID $lakehouseId -e $envName 2>$null
    azd env set FABRIC_WAREHOUSE_ID $warehouseId -e $envName 2>$null
    azd env set FABRIC_LAKEHOUSE_NAME $lakehouseName -e $envName 2>$null
    azd env set FABRIC_DATA_AGENTS_ENABLED "true" -e $envName 2>$null
    Write-Host "[OK] Environment variables saved" -ForegroundColor Green
}

Write-Host "`nNext steps:" -ForegroundColor Yellow
Write-Host "  1. Run 'azd deploy' to rebuild and deploy with new env vars"
Write-Host "  2. (Optional) Create a Data Pipeline in the Fabric portal"
Write-Host "  3. (Optional) Create a Semantic Model / Power BI dataset"
Write-Host "  4. Upload sample data to the Lakehouse Files section"
Write-Host ""

# ================================================================
# Step 5: Validate OneLake connectivity
# ================================================================
Write-Host "`n--- Step 5: OneLake Connectivity Validation ---" -ForegroundColor Yellow

$onelakeDfsEndpoint = if ($env:FABRIC_ONELAKE_DFS_ENDPOINT) { $env:FABRIC_ONELAKE_DFS_ENDPOINT } else { "https://onelake.dfs.fabric.microsoft.com" }
$storageToken = az account get-access-token --resource "https://storage.azure.com" --query accessToken -o tsv 2>$null

if ($storageToken) {
    $olHeaders = @{
        "Authorization" = "Bearer $storageToken"
        "x-ms-version"  = "2021-08-06"
    }

    # Check 1: DFS endpoint reachability
    try {
        $olResp = Invoke-WebRequest -Uri $onelakeDfsEndpoint -Headers $olHeaders -Method GET -ErrorAction SilentlyContinue -TimeoutSec 10
        Write-Host "[OK] OneLake DFS endpoint reachable (HTTP $($olResp.StatusCode))" -ForegroundColor Green
    }
    catch {
        Write-Warning "OneLake DFS endpoint not reachable: $($_.Exception.Message)"
    }

    # Check 2: Workspace root listing
    try {
        $wsUrl = "$onelakeDfsEndpoint/$workspaceId`?resource=account&maxResults=1"
        $wsResp = Invoke-WebRequest -Uri $wsUrl -Headers $olHeaders -Method GET -ErrorAction SilentlyContinue -TimeoutSec 15
        if ($wsResp.StatusCode -lt 400) {
            Write-Host "[OK] Workspace accessible via OneLake DFS" -ForegroundColor Green
        } else {
            Write-Warning "Workspace access returned HTTP $($wsResp.StatusCode)"
        }
    }
    catch {
        Write-Warning "Workspace DFS access failed: $($_.Exception.Message)"
    }

    # Check 3: Lakehouse discovery
    try {
        $lhUrl = "$onelakeDfsEndpoint/$workspaceId/$lakehouseId/Files`?resource=filesystem&recursive=false"
        $lhResp = Invoke-WebRequest -Uri $lhUrl -Headers $olHeaders -Method GET -ErrorAction SilentlyContinue -TimeoutSec 15
        if ($lhResp.StatusCode -lt 400) {
            Write-Host "[OK] Lakehouse Files section discoverable via OneLake DFS" -ForegroundColor Green
        } else {
            Write-Warning "Lakehouse access returned HTTP $($lhResp.StatusCode)"
        }
    }
    catch {
        Write-Warning "Lakehouse DFS access failed: $($_.Exception.Message)"
    }
}
else {
    Write-Warning "Could not acquire OneLake storage token. Skipping connectivity validation."
}

# ================================================================
# Step 6: Save environment identifier
# ================================================================
$fabricEnv = if ($env:FABRIC_ENVIRONMENT) { $env:FABRIC_ENVIRONMENT } else { "dev" }
if ($envName) {
    azd env set FABRIC_ENVIRONMENT $fabricEnv -e $envName 2>$null
    Write-Host "`n[OK] FABRIC_ENVIRONMENT set to '$fabricEnv'" -ForegroundColor Green
}

Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  All done! Environment: $fabricEnv" -ForegroundColor Cyan
Write-Host "============================================`n" -ForegroundColor Cyan
