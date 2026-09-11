> [!CAUTION]
> These exercises have not been tested. There could be incorrect or missing instructions.

# Optional Exercise: Azure Copilot Integration

**Objective:** Connect your MCP-compliant agents to Azure Copilot so users can interact with them through natural language in the Azure portal.

**Duration:** 1 hour

---

## Overview

Azure Copilot provides a natural language interface within the Azure portal. By registering your agents as skills, users can invoke agent tools directly from the Copilot experience — all while maintaining the same governance, identity, and observability provided by the Azure Agents Control Plane.

| Capability | Description |
|------------|-------------|
| **Natural Language Access** | Users invoke agent tools through conversational prompts in the Azure portal |
| **Skill Registration** | Register MCP agents as Azure Copilot skills via APIM |
| **Governed Routing** | All requests flow through APIM policies before reaching agents |
| **Audit Trail** | Every Copilot-initiated action is logged and traceable |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Azure Portal                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                   Azure Copilot                        │  │
│  │   "What is the next best action for customer 12345?"   │  │
│  └───────────────────────┬────────────────────────────────┘  │
└──────────────────────────┼───────────────────────────────────┘
                           │
                           ▼
                 ┌──────────────────┐
                 │   Azure APIM     │
                 │  (MCP Gateway)   │
                 │  - OAuth         │
                 │  - Rate Limits   │
                 │  - Logging       │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │   AKS Cluster    │
                 │  ┌────────────┐  │
                 │  │ MCP Agents │  │
                 │  │  (Pods)    │  │
                 │  └────────────┘  │
                 └──────────────────┘
```

---

## Prerequisites

- Completed Exercise 2 (at least one agent deployed to AKS)
- Azure Agents Control Plane infrastructure deployed
- Azure Copilot enabled on your Azure subscription
- APIM instance configured with MCP endpoints

---

## Part A: Prepare Your Agent for Copilot

### Step A.1: Verify Agent Health and Tool Discovery

Before registering with Copilot, confirm your agent is reachable and exposes tools correctly.

```powershell
# Verify your agent pod is running
kubectl get pods -n mcp-agents

# Port-forward to test locally
kubectl port-forward -n mcp-agents svc/mcp-agents 8000:80
```

In a separate terminal, confirm the MCP tools/list endpoint responds:

```powershell
# Test MCP initialize
$body = @{
    jsonrpc = "2.0"
    id = 1
    method = "initialize"
    params = @{
        protocolVersion = "2024-11-05"
        capabilities = @{}
        clientInfo = @{ name = "copilot-test"; version = "1.0.0" }
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://localhost:8000/mcp" -Method POST -Body $body -ContentType "application/json"
```

```powershell
# Test MCP tools/list
$body = @{
    jsonrpc = "2.0"
    id = 2
    method = "tools/list"
    params = @{}
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://localhost:8000/mcp" -Method POST -Body $body -ContentType "application/json"
```

**Expected:** A JSON-RPC response listing all your agent's tools with their names, descriptions, and input schemas.

### Step A.2: Ensure Tool Descriptions Are Copilot-Friendly

Azure Copilot uses tool descriptions to match user intent to the correct tool. Review your tool definitions and ensure:

- **Descriptions are clear and action-oriented** (e.g., "Analyze customer churn risk and recommend retention actions")
- **Parameter descriptions explain expected values** (e.g., "Customer ID in the format CUST-XXXXX")
- **Tool names use snake_case** and are self-explanatory

If your descriptions need improvement, update them in your agent source code and redeploy:

```powershell
# Rebuild and push updated image
.\scripts\build-and-push.ps1

# Restart the deployment to pick up changes
kubectl rollout restart deployment/mcp-agents -n mcp-agents
```

---

## Part B: Register Agent as a Copilot Skill

### Step B.1: Create a Copilot Skill Manifest

Create a skill manifest that describes your agent's capabilities to Azure Copilot.

Create an optional Copilot skill manifest under the renamed agent-approvals manifests directory:

```json
{
    "schemaVersion": "1.0",
    "skill": {
        "id": "azure-agents-control-plane",
        "name": "Azure Agents Control Plane",
        "description": "Enterprise AI agents governed by Azure APIM, deployed on AKS with MCP protocol support.",
        "version": "1.0.0",
        "endpoint": {
            "type": "mcp",
            "url": "https://<your-apim-gateway>.azure-api.net/mcp",
            "authentication": {
                "type": "oauth2",
                "authority": "https://login.microsoftonline.com/<tenant-id>",
                "clientId": "<copilot-app-registration-client-id>",
                "scope": "api://<apim-app-id>/.default"
            }
        },
        "capabilities": [
            "tools/list",
            "tools/call"
        ]
    }
}
```

### Step B.2: Register the App Registration for Copilot

Create an Entra ID app registration that Azure Copilot will use to authenticate against your APIM:

```powershell
# Create app registration for Copilot skill
az ad app create --display-name "Copilot-AgentsControlPlane-Skill" `
    --sign-in-audience AzureADMyOrg

# Note the appId from the output
$copilotAppId = "<appId-from-output>"

# Add API permission to your APIM app registration
az ad app permission add --id $copilotAppId `
    --api "<apim-app-registration-id>" `
    --api-permissions "<scope-id>=Scope"

# Grant admin consent
az ad app permission admin-consent --id $copilotAppId
```

### Step B.3: Add APIM Policy for Copilot Requests

Add a named value and policy to identify and authorize Copilot-originated requests:

```xml
<!-- Add to your APIM MCP API inbound policy -->
<inbound>
    <base />
    <set-header name="X-Request-Source" exists-action="override">
        <value>azure-copilot</value>
    </set-header>
    <validate-jwt header-name="Authorization" failed-validation-httpcode="401">
        <openid-config url="https://login.microsoftonline.com/{{tenant-id}}/v2.0/.well-known/openid-configuration" />
        <audiences>
            <audience>api://{{apim-app-id}}</audience>
        </audiences>
        <issuers>
            <issuer>https://login.microsoftonline.com/{{tenant-id}}/v2.0</issuer>
        </issuers>
    </validate-jwt>
</inbound>
```

---

## Part C: Test the Integration

### Step C.1: Test via Azure Portal Copilot

1. Open the [Azure portal](https://portal.azure.com)
2. Click the **Copilot** icon in the top navigation bar
3. Try a natural language prompt that maps to one of your agent's tools:

   ```
   What is the next best action for customer CUST-10042?
   ```

4. Azure Copilot should route the request through APIM to your MCP agent and return the result.

### Step C.2: Verify Governance and Tracing

Confirm the request was governed and logged:

```powershell
# Check APIM request logs
az monitor app-insights query --app <app-insights-name> `
    --resource-group <rg> `
    --analytics-query "
        requests
        | where timestamp > ago(15m)
        | where customDimensions['X-Request-Source'] == 'azure-copilot'
        | project timestamp, name, resultCode, duration, customDimensions
        | order by timestamp desc
        | take 10
    "
```

### Step C.3: Verify in Cosmos DB

Check that the Copilot-initiated session was recorded:

```powershell
# Query Cosmos DB for recent sessions
az cosmosdb sql query --account-name <cosmos-account> `
    --database-name agents `
    --container-name sessions `
    --resource-group <rg> `
    --query-text "SELECT TOP 5 * FROM c WHERE c.source = 'azure-copilot' ORDER BY c._ts DESC"
```

---

## Verification Checklist

Before completing this exercise, confirm:

- [ ] Agent tools are discoverable via MCP `tools/list`
- [ ] Tool descriptions are clear and Copilot-friendly
- [ ] Copilot skill manifest created with correct endpoint and auth
- [ ] App registration configured for Copilot-to-APIM authentication
- [ ] APIM policy validates Copilot JWT tokens
- [ ] Natural language prompts in Azure Copilot invoke your agent tools
- [ ] Requests are logged in Application Insights with Copilot source
- [ ] Sessions are recorded in Cosmos DB

---

## Summary

In this exercise you:

1. **Prepared** your MCP agent for Copilot integration by verifying tool discovery and improving descriptions
2. **Registered** your agent as an Azure Copilot skill with proper authentication
3. **Configured** APIM policies to authorize and trace Copilot-originated requests
4. **Tested** end-to-end natural language interaction from Azure Copilot to your agents

Your agents are now accessible through the Azure portal's Copilot experience while maintaining full enterprise governance, identity, and observability through the Azure Agents Control Plane.

---

**[Back to Lab Manual →](../LAB_MANUAL_BUILD_YOUR_OWN_AGENT.md)**
