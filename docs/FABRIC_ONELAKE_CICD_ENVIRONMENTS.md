# Fabric OneLake & CICD Environments — Configuration Guide

This guide covers the Fabric workspace configuration, OneLake connectivity validation, and the Dev/Test/Prod CICD environment setup for the **azure-agents-control-plane** project.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Fabric Workspace Setup](#2-fabric-workspace-setup)
3. [OneLake Connectivity Validation](#3-onelake-connectivity-validation)
4. [Environment Configuration (Dev / Test / Prod)](#4-environment-configuration-dev--test--prod)
5. [CICD Pipelines](#5-cicd-pipelines)
6. [Kubernetes Environment Overlays](#6-kubernetes-environment-overlays)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Overview

The project uses **Microsoft Fabric** with **OneLake** as the unified data lake. Three separate environments are maintained:

| Environment | Fabric SKU | Replicas | VNet | Defender | Purview |
|-------------|-----------|----------|------|----------|---------|
| **Dev**     | F2        | 1        | No   | No       | No      |
| **Test**    | F4        | 2        | Yes  | Yes      | No      |
| **Prod**    | F8        | 3        | Yes  | Yes      | Yes     |

Each environment has:
- Its own Fabric workspace (e.g., `agents-dev-ws`, `agents-test-ws`, `agents-prod-ws`)
- Separate `azd` environment with environment-specific Bicep parameters
- Kubernetes overlay for resource scaling and environment labels

---

## 2. Fabric Workspace Setup

### 2.1 Prerequisites

| Requirement | Details |
|-------------|---------|
| Azure Subscription | Active, with Contributor role |
| Fabric License | F-SKU capacity (F2 minimum) |
| Azure CLI | `az login` authenticated |
| Azure Developer CLI | `azd version` >= 1.0 |

### 2.2 Provision per environment

```powershell
# Dev environment
azd env new agents-dev --no-prompt
azd env set FABRIC_SKU F2
azd env set FABRIC_ENVIRONMENT dev
azd provision --no-prompt
pwsh ./scripts/deploy-fabric-workspace.ps1 -WorkspaceName "agents-dev-ws"

# Test environment
azd env new agents-test --no-prompt
azd env set FABRIC_SKU F4
azd env set FABRIC_ENVIRONMENT test
azd provision --no-prompt
pwsh ./scripts/deploy-fabric-workspace.ps1 -WorkspaceName "agents-test-ws"

# Prod environment
azd env new agents-prod --no-prompt
azd env set FABRIC_SKU F8
azd env set FABRIC_ENVIRONMENT prod
azd env set PURVIEW_ENABLED true
azd env set AGENT_IDENTITY_ENABLED true
azd provision --no-prompt
pwsh ./scripts/deploy-fabric-workspace.ps1 -WorkspaceName "agents-prod-ws"
```

### 2.3 Workspace reference

After provisioning, the following `azd env` variables are saved automatically:

| Variable | Description |
|----------|-------------|
| `FABRIC_WORKSPACE_ID` | Workspace GUID |
| `FABRIC_LAKEHOUSE_ID` | Lakehouse item GUID |
| `FABRIC_WAREHOUSE_ID` | Warehouse item GUID |
| `FABRIC_LAKEHOUSE_NAME` | Lakehouse display name |
| `FABRIC_ENVIRONMENT` | `dev`, `test`, or `prod` |

---

## 3. OneLake Connectivity Validation

### 3.1 Automated validation

The `OneLakeClient.validate_connection()` method runs three checks:

1. **Token acquisition** — Acquires a DFS token via `DefaultAzureCredential`
2. **Endpoint reachability** — Sends an HTTP request to the OneLake DFS endpoint
3. **Workspace access** — Lists the workspace root to confirm RBAC permissions

### 3.2 Run validation manually

```python
import os, json, sys
sys.path.insert(0, "src")
os.environ["FABRIC_ENVIRONMENT"] = "dev"  # or test, prod

from fabric_onelake import onelake_validate_connection_tool
result = json.loads(onelake_validate_connection_tool())
print(json.dumps(result, indent=2))
```

### 3.3 Run via MCP tool

```json
{
  "tool": "onelake_validate_connection_tool",
  "parameters": {}
}
```

### 3.4 Discover OneLake items

```json
{
  "tool": "onelake_discover_items_tool",
  "parameters": {
    "lakehouse_id": "<FABRIC_LAKEHOUSE_ID>"
  }
}
```

### 3.5 Expected output (healthy)

```json
{
  "success": true,
  "checks": [
    {"check": "token_acquisition", "success": true, "message": "Successfully acquired OneLake DFS token"},
    {"check": "endpoint_reachable", "success": true, "message": "DFS endpoint returned HTTP 200"},
    {"check": "workspace_access", "success": true, "message": "Workspace is accessible via OneLake DFS"}
  ],
  "environment": "dev",
  "workspace_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "dfs_endpoint": "https://onelake.dfs.fabric.microsoft.com"
}
```

---

## 4. Environment Configuration (Dev / Test / Prod)

### 4.1 Bicep parameter files

| File | Environment | Key differences |
|------|-------------|-----------------|
| `infra/main.parameters.dev.json` | Dev | F2, VNet off, Defender off |
| `infra/main.parameters.test.json` | Test | F4, VNet on, Defender on |
| `infra/main.parameters.prod.json` | Prod | F8, VNet on, Defender on, Purview on, Agent Identity on |

### 4.2 Switching environments

```powershell
# Switch to test
azd env select agents-test

# Verify current env
azd env get-values
```

### 4.3 Environment variable: `FABRIC_ENVIRONMENT`

Set `FABRIC_ENVIRONMENT` to `dev`, `test`, or `prod`. This variable:
- Is injected into the K8s deployment via env vars
- Is read by `OneLakeClient` for logging and validation reporting
- Determines workspace naming conventions

---

## 5. CICD Pipelines

### 5.1 GitHub Actions workflows

| Workflow | File | Trigger | Environment |
|----------|------|---------|-------------|
| CI | `.github/workflows/ci.yml` | Push/PR to `main` | — |
| Deploy Dev | `.github/workflows/deploy-dev.yml` | Push to `main` | `dev` |
| Deploy Test | `.github/workflows/deploy-test.yml` | Manual (with confirmation) | `test` |
| Deploy Prod | `.github/workflows/deploy-prod.yml` | Manual (with confirmation) | `production` |

### 5.2 Required GitHub secrets

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | Service principal / federated identity client ID |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Target subscription ID |

### 5.3 Deployment flow

```
PR → CI (lint + test + docker build)
       ↓
merge to main → Deploy Dev (auto)
                    ↓
             Deploy Test (manual trigger, confirmation required)
                    ↓
             Deploy Prod (manual trigger, confirmation + approval gate)
```

Each deployment:
1. Provisions infrastructure via `azd provision`
2. Deploys the Fabric workspace via `deploy-fabric-workspace.ps1`
3. Validates OneLake connectivity
4. Builds and deploys to AKS via `azd deploy`

---

## 6. Kubernetes Environment Overlays

The project uses **Kustomize** for environment-specific K8s configurations.

### Directory structure

```
k8s/
├── base/
│   └── kustomization.yaml        # References the base deployment
├── mcp-agents-deployment.yaml    # Base deployment manifest
└── overlays/
    ├── dev/
    │   └── kustomization.yaml    # 1 replica, low resources
    ├── test/
    │   └── kustomization.yaml    # 2 replicas, standard resources
    └── prod/
        ├── kustomization.yaml    # 3 replicas, high resources
        └── pdb.yaml              # PodDisruptionBudget (minAvailable: 2)
```

### Apply an overlay

```bash
# Dev
kubectl apply -k k8s/overlays/dev/

# Test
kubectl apply -k k8s/overlays/test/

# Prod
kubectl apply -k k8s/overlays/prod/
```

---

## 7. Troubleshooting

### OneLake token acquisition fails

```
Check: token_acquisition — Failed
```

- Ensure `az login` is authenticated or workload identity is configured
- Verify `DefaultAzureCredential` can access the `https://storage.azure.com/.default` scope
- In AKS, confirm the service account has valid federated credential annotations

### DFS endpoint unreachable

```
Check: endpoint_reachable — Failed
```

- Check network/firewall rules; OneLake DFS uses `https://onelake.dfs.fabric.microsoft.com`
- In VNet-enabled environments, ensure the private endpoint for OneLake is configured
- Verify DNS resolution: `nslookup onelake.dfs.fabric.microsoft.com`

### Workspace access denied

```
Check: workspace_access — Failed (HTTP 403)
```

- Ensure the identity has **Contributor or higher** on the Fabric workspace
- Verify the workspace ID matches: `azd env get-values | grep FABRIC_WORKSPACE_ID`
- Check workspace state in the Fabric portal (must be Active)

### No items discovered

```
discover_items: total_items=0
```

- Upload sample data: `pwsh ./scripts/upload-ontologies-to-onelake.ps1`
- Create a Lakehouse table via Fabric portal or Spark notebook
- Check the Lakehouse ID: `azd env get-values | grep FABRIC_LAKEHOUSE_ID`
