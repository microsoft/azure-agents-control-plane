# Fabric Data Agents — Complete Setup Guide

A step-by-step guide to connect and create Fabric Data Agents that interact with a Microsoft Fabric Lakehouse workspace.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Authenticate to Azure](#2-authenticate-to-azure)
3. [Provision Fabric Capacity](#3-provision-fabric-capacity)
4. [Resume / Manage Capacity](#4-resume--manage-capacity)
5. [Create Fabric Workspace & Lakehouse](#5-create-fabric-workspace--lakehouse)
6. [Upload Sample Data to Lakehouse](#6-upload-sample-data-to-lakehouse)
7. [Configure Environment Variables](#7-configure-environment-variables)
8. [Build & Deploy the MCP Container](#8-build--deploy-the-mcp-container)
9. [Test the Fabric Data Agents](#9-test-the-fabric-data-agents)
10. [MCP Tool Reference](#10-mcp-tool-reference)
11. [Architecture Overview](#11-architecture-overview)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Requirement | Details |
|-------------|---------|
| **Azure Subscription** | Active subscription with `Contributor` role |
| **Azure CLI** | v2.50+ (`az --version`) |
| **PowerShell** | 7.x+ (`pwsh --version`) |
| **Python** | 3.11+ |
| **Docker** | For building container images |
| **kubectl** | For AKS deployment |
| **Azure Developer CLI (azd)** | For IaC provisioning (`azd version`) |
| **Fabric License** | F-SKU capacity (F2, F4, F8…) — Premium Per User (PP3) does **not** support Lakehouse |

### Verify Prerequisites

```powershell
az --version
python --version
docker --version
kubectl version --client
azd version
```

---

## 2. Authenticate to Azure

### 2.1 Login to the correct tenant

```powershell
# Replace with your tenant ID
az login --tenant <YOUR_TENANT_ID>

# Example:
az login --tenant 6fc3d6e0-df56-4271-ab53-9782cdea9bf6
```

### 2.2 Verify you're on the right subscription

```powershell
az account show --query "{name:name, id:id, tenantId:tenantId, user:user.name}" -o table
```

### 2.3 Switch subscription if needed

```powershell
az account set --subscription "<SUBSCRIPTION_ID>"
```

---

## 3. Provision Fabric Capacity

Fabric items (Lakehouse, Warehouse, Pipeline, Semantic Model) require an **F-SKU** capacity.

> **Important**: Premium Per User (PP3/P-SKU) does **not** support Lakehouse or Warehouse creation. You need an F-SKU (F2 minimum).

### Option A: Deploy via Bicep (recommended)

The repository includes a Bicep template at `infra/core/fabric/fabric-capacity.bicep`:

```powershell
# Create a resource group (or use an existing one)
az group create --name rg-fabric-agents --location westus3

# Deploy an F2 capacity (smallest, ~$0.36/hr)
az deployment group create `
    --resource-group "rg-fabric-agents" `
    --template-file "./infra/core/fabric/fabric-capacity.bicep" `
    --parameters name="fabricagentsf2" `
                 location="westus3" `
                 skuName="F2" `
                 adminMembers="['yourname@yourdomain.com']"
```

> **Note**: The capacity name must contain only lowercase letters and numbers (no hyphens/underscores).

### Option B: Create via Azure Portal

1. Go to [Azure Portal](https://portal.azure.com)
2. Search for **Microsoft Fabric** → **Create**
3. Select SKU: **F2** (or higher)
4. Select region and admin members
5. Click **Create**

### 3.1 Verify the capacity

```powershell
az resource list --resource-type "Microsoft.Fabric/capacities" -o table
```

---

## 4. Resume / Manage Capacity

Fabric F-SKU capacities can be **paused** to stop billing and **resumed** when needed.

### List capacity states via Fabric API

```powershell
$token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
$caps = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/capacities" -Headers $headers
$caps.value | Select-Object displayName, sku, state, id | Format-Table -AutoSize
```

### Resume a paused capacity

```powershell
az resource invoke-action `
    --resource-group "<RESOURCE_GROUP>" `
    --name "<CAPACITY_NAME>" `
    --resource-type "Microsoft.Fabric/capacities" `
    --action resume

# Example:
az resource invoke-action --resource-group "rg-sdemo" --name "saifabric01" --resource-type "Microsoft.Fabric/capacities" --action resume
```

Wait ~15 seconds for activation, then verify:

```powershell
Start-Sleep -Seconds 15
# Re-run the capacity list above to confirm state = "Active"
```

### Pause capacity (stop billing)

```powershell
az resource invoke-action `
    --resource-group "<RESOURCE_GROUP>" `
    --name "<CAPACITY_NAME>" `
    --resource-type "Microsoft.Fabric/capacities" `
    --action suspend
```

---

## 5. Create Fabric Workspace & Lakehouse

### Option A: Use the automated deploy script (recommended)

The repository includes `scripts/deploy-fabric-workspace.ps1` which creates:
- A Fabric **Workspace**
- A **Lakehouse** (AgentsLakehouse)
- A **Warehouse** (AgentsWarehouse)

```powershell
cd azure-agents-control-plane

# Get your capacity ID from Step 4 above
./scripts/deploy-fabric-workspace.ps1 `
    -CapacityId "<YOUR_F_SKU_CAPACITY_ID>" `
    -WorkspaceName "agents-fabric-data-ws"

# Example:
./scripts/deploy-fabric-workspace.ps1 `
    -CapacityId "6d791cc5-92b9-4b3f-ba6d-fecddea68e11" `
    -WorkspaceName "agents-fabric-data-ws"
```

**Expected output:**

```
============================================
  Fabric Data Agents - Workspace Provisioning
============================================

--- Step 1: Workspace ---
[OK] Workspace created: 4625b2e9-2493-4855-a357-592f36755ebd

--- Step 2: Lakehouse ---
[OK] Lakehouse created: 684658fa-5036-4075-a78e-bd8dfb8df02e

--- Step 3: Warehouse ---
[OK] Warehouse created: <warehouse-id>

============================================
  Provisioning Complete!
============================================
Resource IDs:
  FABRIC_WORKSPACE_ID  = 4625b2e9-2493-4855-a357-592f36755ebd
  FABRIC_LAKEHOUSE_ID  = 684658fa-5036-4075-a78e-bd8dfb8df02e
```

Save these IDs — you'll need them for environment variables.

### Option B: Create manually via Fabric Portal

1. Open [Power BI / Fabric portal](https://app.powerbi.com)
2. Click **Workspaces** → **New Workspace**
3. Name: `agents-fabric-data-ws`
4. Under **Advanced** → **Capacity**, select your F-SKU capacity
5. Click **Apply**
6. Inside the workspace, click **+ New item** → **Lakehouse**
7. Name: `AgentsLakehouse`
8. Note the Workspace ID from the URL: `https://app.powerbi.com/groups/<WORKSPACE_ID>/...`

### 5.1 Reassign workspace to a different capacity

```powershell
$token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

Invoke-RestMethod -Method POST `
    -Uri "https://api.fabric.microsoft.com/v1/workspaces/<WORKSPACE_ID>/assignToCapacity" `
    -Headers $headers `
    -Body (@{ capacityId = "<NEW_CAPACITY_ID>" } | ConvertTo-Json)
```

### 5.2 Verify workspace items

```powershell
$token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }

# List workspaces
$ws = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Headers $headers
$ws.value | Format-Table id, displayName, capacityId -AutoSize

# List items in workspace
$wsId = "<YOUR_WORKSPACE_ID>"
$items = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$wsId/items" -Headers $headers
$items.value | Format-Table id, displayName, type -AutoSize
```

---

## 6. Upload Sample Data to Lakehouse

Upload data files to the Lakehouse's **Files** section via OneLake.

### Using the Fabric Portal

1. Open your workspace in [Fabric Portal](https://app.powerbi.com)
2. Open **AgentsLakehouse**
3. Click **Files** → **Upload** → **Upload files**
4. Upload CSV/Parquet/JSON files

### Using the OneLake REST API (programmatic)

```powershell
$token = az account get-access-token --resource "https://storage.azure.com" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/octet-stream" }
$wsId = "<WORKSPACE_ID>"
$lhId = "<LAKEHOUSE_ID>"
$fileName = "customers.csv"
$dfsBase = "https://onelake.dfs.fabric.microsoft.com"

# Step 1: Create the file
Invoke-RestMethod -Method PUT `
    -Uri "$dfsBase/$wsId/$lhId/Files/$fileName`?resource=file" `
    -Headers $headers

# Step 2: Append data
$bytes = [System.IO.File]::ReadAllBytes(".\data\customers.csv")
Invoke-RestMethod -Method PATCH `
    -Uri "$dfsBase/$wsId/$lhId/Files/$fileName`?action=append&position=0" `
    -Headers $headers `
    -Body $bytes

# Step 3: Flush (commit)
Invoke-RestMethod -Method PATCH `
    -Uri "$dfsBase/$wsId/$lhId/Files/$fileName`?action=flush&position=$($bytes.Length)" `
    -Headers $headers
```

### Using Python (via fabric_onelake.py)

```python
from fabric_onelake import get_onelake_client

client = get_onelake_client()
result = client.write_file(
    item_id="<LAKEHOUSE_ID>",
    path="customers.csv",
    data=open("data/customers.csv", "rb").read(),
    content_type="text/csv"
)
print(result)
```

---

## 7. Configure Environment Variables

### 7.1 Required environment variables

Set these in your deployment configuration:

| Variable | Description | Example |
|----------|-------------|---------|
| `FABRIC_ENABLED` | Enable Fabric integration | `true` |
| `FABRIC_DATA_AGENTS_ENABLED` | Enable Fabric Data Agents | `true` |
| `FABRIC_WORKSPACE_ID` | Workspace GUID from Step 5 | `4625b2e9-2493-4855-a357-592f36755ebd` |
| `FABRIC_API_ENDPOINT` | Fabric REST API base URL | `https://api.fabric.microsoft.com/v1` |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | OneLake DFS endpoint | `https://onelake.dfs.fabric.microsoft.com` |
| `FABRIC_AGENT_MAX_RETRIES` | Max retries for pipeline operations | `3` |
| `FABRIC_PIPELINE_POLL_INTERVAL` | Pipeline status poll interval (seconds) | `30` |
| `FABRIC_PIPELINE_TIMEOUT` | Pipeline run timeout (seconds) | `3600` |
| `ONELAKE_MAX_DOWNLOAD_BYTES` | Max file download size (bytes) | `52428800` (50 MB) |

### 7.2 Set via azd environment

```powershell
azd env set FABRIC_ENABLED "true"
azd env set FABRIC_DATA_AGENTS_ENABLED "true"
azd env set FABRIC_WORKSPACE_ID "4625b2e9-2493-4855-a357-592f36755ebd"
azd env set FABRIC_LAKEHOUSE_ID "684658fa-5036-4075-a78e-bd8dfb8df02e"
azd env set FABRIC_API_ENDPOINT "https://api.fabric.microsoft.com/v1"
azd env set FABRIC_ONELAKE_DFS_ENDPOINT "https://onelake.dfs.fabric.microsoft.com"
```

### 7.3 Set for local development

Create a `.env` file or set system environment variables:

```bash
export FABRIC_ENABLED=true
export FABRIC_DATA_AGENTS_ENABLED=true
export FABRIC_WORKSPACE_ID=4625b2e9-2493-4855-a357-592f36755ebd
export FABRIC_API_ENDPOINT=https://api.fabric.microsoft.com/v1
export FABRIC_ONELAKE_DFS_ENDPOINT=https://onelake.dfs.fabric.microsoft.com
```

---

## 8. Build & Deploy the MCP Container

### 8.1 Build the Docker image

```powershell
cd src

# Build the image
docker build -t mcp-agents:latest .

# Tag for Azure Container Registry
$acr = "<YOUR_ACR_NAME>.azurecr.io"
docker tag mcp-agents:latest "$acr/mcp-agents:latest"
```

### 8.2 Push to ACR

```powershell
az acr login --name <YOUR_ACR_NAME>
docker push "$acr/mcp-agents:latest"
```

### 8.3 Deploy to AKS via azd

```powershell
# Full provision + deploy (infrastructure + application)
azd up

# Or deploy only the application (if infrastructure already exists)
azd deploy
```

### 8.4 Deploy manually to AKS

```powershell
# Get AKS credentials
az aks get-credentials --resource-group <RG_NAME> --name <AKS_NAME> --overwrite-existing

# Apply configured K8s manifests
kubectl apply -f ./k8s/mcp-agents-deployment-configured.yaml
kubectl apply -f ./k8s/mcp-agents-loadbalancer-configured.yaml

# Wait for rollout
kubectl rollout status deployment/mcp-agents -n mcp-agents --timeout=300s
```

### 8.5 Verify deployment

```powershell
kubectl get pods -n mcp-agents
kubectl logs -l app=mcp-agents -n mcp-agents --tail=50
```

---

## 9. Test the Fabric Data Agents

### 9.1 Run unit tests

```powershell
cd azure-agents-control-plane
python -m pytest tests/test_fabric_data_agents.py -v
```

### 9.2 Run integration tests

```powershell
python -m pytest tests/test_fabric_agents.py -v --use-az-token
```

### 9.3 Test via MCP tool calls

Once deployed, interact with the agents via MCP SSE endpoint:

**Query Lakehouse:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "fabric_agent_query",
    "arguments": {
      "user_query": "Show me the top 10 customers by monthly spend from the lakehouse",
      "agent_type": "lakehouse"
    }
  },
  "id": 1
}
```

**List all Fabric resources:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "fabric_agent_list_all",
    "arguments": {}
  },
  "id": 2
}
```

**Upload a file to OneLake:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "onelake_write_file",
    "arguments": {
      "item_id": "<LAKEHOUSE_ID>",
      "path": "data/report.csv",
      "content": "id,name,value\n1,Alpha,100\n2,Beta,200"
    }
  },
  "id": 3
}
```

**Read a file from OneLake:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "onelake_read_file",
    "arguments": {
      "item_id": "<LAKEHOUSE_ID>",
      "path": "data/report.csv"
    }
  },
  "id": 4
}
```

**Cross-domain query:**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "fabric_agent_cross_domain",
    "arguments": {
      "user_query": "Get customer churn data from lakehouse and trigger the refresh pipeline"
    }
  },
  "id": 5
}
```

---

## 10. MCP Tool Reference

### Fabric Data Agent Tools

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `fabric_agent_query` | Orchestrated query — auto-routes to the right agent | `user_query`, `agent_type` (optional), `lakehouse_id`, `warehouse_id`, `dataset_id`, `pipeline_id` |
| `fabric_agent_cross_domain` | Multi-agent cross-domain query | `user_query` |
| `fabric_agent_list_all` | List all Fabric resources in workspace | _(none)_ |

### OneLake File Tools

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `onelake_list_files` | List files/folders in a Lakehouse | `item_id`, `path` (optional) |
| `onelake_read_file` | Read a file from OneLake | `item_id`, `path` |
| `onelake_write_file` | Write/upload a file to OneLake | `item_id`, `path`, `content` |
| `onelake_delete_file` | Delete a file from OneLake | `item_id`, `path` |
| `onelake_get_file_properties` | Get file metadata (size, date) | `item_id`, `path` |

### Agent Types (for `fabric_agent_query`)

| Agent Type | Use Case | Query Language |
|------------|----------|----------------|
| `lakehouse` | Delta tables, Spark SQL queries | Spark SQL |
| `warehouse` | Structured analytics, T-SQL queries | T-SQL |
| `pipeline` | Trigger/monitor ETL pipelines | N/A |
| `semantic_model` | Power BI datasets, KPIs | DAX / MDX |

---

## 11. Architecture Overview

```
                    ┌─────────────────────────────────────┐
                    │         MCP Server (FastAPI)         │
                    │    next_best_action_agent.py         │
                    │         Port 8000 (AKS)             │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │     FabricAgentOrchestrator          │
                    │         fabric_agents.py             │
                    │   Routes requests by keyword/type    │
                    └──┬──────┬──────┬──────┬─────────────┘
                       │      │      │      │
          ┌────────────▼┐  ┌──▼────┐ ┌▼─────┐ ┌▼────────────┐
          │  Lakehouse  │  │Ware-  │ │Pipe- │ │  Semantic   │
          │   Agent     │  │house  │ │line  │ │   Model     │
          │ (Spark SQL) │  │Agent  │ │Agent │ │   Agent     │
          │             │  │(T-SQL)│ │      │ │  (DAX/MDX)  │
          └──────┬──────┘  └──┬────┘ └──┬───┘ └──────┬──────┘
                 │            │         │            │
          ┌──────▼────────────▼─────────▼────────────▼──────┐
          │              fabric_tools.py                     │
          │         Low-level Fabric REST API calls          │
          │    https://api.fabric.microsoft.com/v1           │
          └─────────────────────┬────────────────────────────┘
                                │
          ┌─────────────────────▼────────────────────────────┐
          │              fabric_onelake.py                    │
          │       OneLake DFS File Operations                │
          │   https://onelake.dfs.fabric.microsoft.com       │
          └──────────────────────────────────────────────────┘
                                │
          ┌─────────────────────▼────────────────────────────┐
          │            Microsoft Fabric                       │
          │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
          │  │Lakehouse │ │Warehouse │ │ Semantic Models  │ │
          │  │(Delta)   │ │ (T-SQL)  │ │ (Power BI)       │ │
          │  └──────────┘ └──────────┘ └──────────────────┘ │
          │  ┌──────────┐ ┌──────────────────────────────┐  │
          │  │Pipelines │ │      OneLake (DFS)           │  │
          │  │ (ETL)    │ │   Unified Data Lake          │  │
          │  └──────────┘ └──────────────────────────────┘  │
          └─────────────────────────────────────────────────┘
```

### Authentication Flow

```
AKS Pod → Workload Identity → Managed Identity → Azure AD Token
  → Fabric API scope: https://analysis.windows.net/powerbi/api/.default
  → OneLake scope:    https://storage.azure.com/.default
```

### Key Files

| File | Purpose |
|------|---------|
| `src/fabric_agents.py` | 4 specialist agents + orchestrator |
| `src/fabric_onelake.py` | OneLake DFS file operations |
| `src/fabric_tools.py` | Low-level Fabric REST API client |
| `src/next_best_action_agent.py` | Main MCP server (registers tools) |
| `scripts/deploy-fabric-workspace.ps1` | Automated Fabric provisioning |
| `infra/core/fabric/fabric-capacity.bicep` | Fabric capacity IaC |
| `infra/app/fabric-data-agents.bicep` | RBAC assignments |
| `k8s/mcp-agents-deployment.yaml` | K8s deployment manifest |
| `tests/test_fabric_data_agents.py` | Unit tests (30+ tests) |

---

## 12. Troubleshooting

### Error: "FeatureNotAvailable" (403) when creating Lakehouse

**Cause**: Workspace is on a PP-SKU (Premium Per User) capacity, not an F-SKU.

**Fix**: Assign the workspace to an F-SKU capacity:

```powershell
# Find your F-SKU capacity ID
$token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
$caps = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/capacities" -Headers $headers
$caps.value | Where-Object { $_.sku -like "F*" } | Format-Table displayName, sku, state, id

# Reassign workspace
$headers["Content-Type"] = "application/json"
Invoke-RestMethod -Method POST `
    -Uri "https://api.fabric.microsoft.com/v1/workspaces/<WS_ID>/assignToCapacity" `
    -Headers $headers `
    -Body (@{ capacityId = "<F_SKU_CAPACITY_ID>" } | ConvertTo-Json)
```

### Error: "CapacityNotInActiveState" (400)

**Cause**: The Fabric capacity is paused/inactive.

**Fix**: Resume the capacity (see [Step 4](#4-resume--manage-capacity)).

### Error: "Invalid chars in resource name"

**Cause**: Fabric capacity names can only contain lowercase letters and numbers.

**Fix**: Use a name like `fabricagentsf2` (no hyphens/underscores).

### Error: "UnicodeDecodeError: 'charmap' codec can't decode byte"

**Cause**: Windows cp1252 encoding issue with special characters in Python files.

**Fix**: Always open files with explicit `encoding='utf-8'`:

```python
ast.parse(open('src/file.py', encoding='utf-8').read())
```

### Lakehouse query returns empty results

**Possible causes**:
1. No data uploaded to the Lakehouse yet → Upload via [Step 6](#6-upload-sample-data-to-lakehouse)
2. Table not created from files → In Fabric Portal, right-click the file → **Load to Tables**
3. Wrong `lakehouse_id` → Verify with `fabric_agent_list_all`

### Pods fail to authenticate to Fabric API

**Cause**: Workload identity not configured.

**Fix**: Ensure federated identity credential exists:

```powershell
az identity federated-credential create `
    --name mcp-agents-federated `
    --identity-name <IDENTITY_NAME> `
    --resource-group <RG_NAME> `
    --issuer <AKS_OIDC_ISSUER> `
    --subject "system:serviceaccount:mcp-agents:mcp-agents-sa" `
    --audience "api://AzureADTokenExchange"
```

### Cannot see Fabric portal

**Fix**: Use the correct URL with your tenant:

```
https://app.powerbi.com/home?ctid=<YOUR_TENANT_ID>
```

---

## Quick Reference: End-to-End Commands

```powershell
# 1. Login
az login --tenant 6fc3d6e0-df56-4271-ab53-9782cdea9bf6

# 2. Resume capacity
az resource invoke-action --resource-group "rg-sdemo" --name "saifabric01" `
    --resource-type "Microsoft.Fabric/capacities" --action resume

# 3. Create workspace + Lakehouse + Warehouse
./scripts/deploy-fabric-workspace.ps1 `
    -CapacityId "6d791cc5-92b9-4b3f-ba6d-fecddea68e11" `
    -WorkspaceName "agents-fabric-data-ws"

# 4. Set env vars
azd env set FABRIC_WORKSPACE_ID "<WORKSPACE_ID>"
azd env set FABRIC_DATA_AGENTS_ENABLED "true"

# 5. Deploy
azd deploy

# 6. Run tests
python -m pytest tests/test_fabric_data_agents.py -v

# 7. Pause capacity when done
az resource invoke-action --resource-group "rg-sdemo" --name "saifabric01" `
    --resource-type "Microsoft.Fabric/capacities" --action suspend
```
