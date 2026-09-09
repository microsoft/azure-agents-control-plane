# Microsoft Agent 365 / Entra Agent Registry

> **Verified implementation (2026-09-09):** The Python `next_best_action`
> deployment/CI-CD approval flow passed a live Teams approval, authenticated
> callback, durable Cosmos validation, and same-request resume test on AKS.
> This page below contains historical design/demo examples, including removed
> APIs; use the [current test report and runbook](AGENTS_APPROVAL_FLOW_VALIDATION.md)
> and [exported activity diagram](AGENTS_APPROVAL_FLOW.mermaid) instead.
> Approval gates recommendation generation for deployment requests, not every
> tool or an automatically executed deployment.

## Agent Registration and Agents Approval System

This folder contains the artifacts for Microsoft Agent 365 registration and the agent approval workflow system with Microsoft Teams human-in-the-loop integration.

> **📖 Full Documentation:** See [docs/AGENTS_APPROVALS.md](../docs/AGENTS_APPROVALS.md) for comprehensive documentation.

---

## Folder Contents

| Path | Description |
|------|-------------|
| [manifests/agent_card_manifest.json](manifests/agent_card_manifest.json) | Agent card manifest for Entra Agent Registry |
| [manifests/agent_instance.json](manifests/agent_instance.json) | Agent instance definition with security profile |
| [teams/agent_approval_card.json](teams/agent_approval_card.json) | Teams Adaptive Card for approval requests |
| [teams/agent_approval_result_card.json](teams/agent_approval_result_card.json) | Teams Adaptive Card for approval results |
| [workflows/agent_approval_logic_app.json](workflows/agent_approval_logic_app.json) | Azure Logic App fallback workflow |

---

## Quick Links

- **Approval Module:** [src/agent365_approval.py](../src/agent365_approval.py)
- **Infrastructure:** [infra/app/agents-approval-logicapp.bicep](../infra/app/agents-approval-logicapp.bicep)
- **Agents Pipeline:** [.github/workflows/deploy-with-approval.yml](../.github/workflows/deploy-with-approval.yml)

---

## Overview

This implementation provides a dual-track approval system for Agents pipeline deployments:

1. **Microsoft Teams Human Approval** - The decision surface where humans review and approve/reject deployments
2. **Agent Approval Workflow** - The agent-mediated workflow that validates, records, and enforces approval decisions

### Key Principles

- **Teams + Agent workflow BOTH required** - Approval is NOT complete unless a human decides in Teams AND the agent validates the decision
- **Clear audit trail** - All approvals are logged to CosmosDB with full context
- **No speculative APIs** - Implementation uses documented Microsoft Graph APIs only

---

## Agent 365 Status

**Status: Frontier Preview**

Microsoft Agent 365 is currently in **Frontier preview**. Organizations must be enrolled in the [Microsoft Frontier preview program](https://adoption.microsoft.com/copilot/frontier-program/) to access Agent 365 capabilities.

---

## References

- [Microsoft Entra Agent Registry](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/what-is-agent-registry)
- [Agent 365 Capabilities](https://learn.microsoft.com/en-us/microsoft-agent-365/admin/capabilities-entra)
- [Teams Approvals API](https://learn.microsoft.com/en-us/graph/approvals-app-api)
- [Microsoft Frontier Program](https://adoption.microsoft.com/copilot/frontier-program/)
  "description": "AI agent that analyzes tasks and generates action plans with semantic reasoning...",
  "capabilities": {
    "supportsA2A": true,
    "supportsMCP": true,
    "supportsHITL": true,
    "supportsApprovalWorkflows": true
  },
  "skills": [
    {
      "id": "agents-approval-workflow",
      "name": "Agents Approval Workflow",
      "description": "Manages human-in-the-loop approvals for Agents pipeline deployments"
    }
  ]
}
```

### Agent Instance Definition

Location: [agent365/manifests/agent_instance.json](manifests/agent_instance.json)

Key configuration:
- **Owner**: Defined via environment variables
- **Environment**: Production (configurable)
- **Security Profile**: StandardDevOpsAgent blueprint with MFA requirement
- **Approval Configuration**: 2-hour SLA, escalation to Platform Engineer

### Required Roles and Permissions

| Permission | Scope | Purpose |
|------------|-------|---------|
| `AgentInstance.ReadWrite.All` | Application | Register/update agent instances |
| `AgentInstance.ReadWrite.ManagedBy` | Application | Manage owned agents |
| `Approvals.ReadWrite.All` | Delegated | Create/manage approval requests |

### Registration API

```bash
# Register agent instance
POST https://graph.microsoft.com/beta/agentRegistry/agentInstances
Authorization: Bearer {token}
Content-Type: application/json

{
  "id": "next-best-action-agent-instance-001",
  "displayName": "Next Best Action Agent",
  "url": "${AGENT_ENDPOINT_URL}",
  "isBlocked": false,
  "originatingStore": "AzureAIFoundry"
}
```

---

## Agent Approval Workflow

### Workflow Engine

The `ApprovalWorkflowEngine` class orchestrates the complete approval process:

```python
from agent365_approval import get_approval_workflow_engine, require_agents_approval

# Option 1: Use the convenience function (blocking)
approval = await require_agents_approval(
    task="Set up a Agents pipeline for deploying microservices to Kubernetes",
    requested_by="developer@contoso.com",
    environment="production",
    cluster="aks-prod-cluster"
)

# Option 2: Use the engine directly (non-blocking)
engine = get_approval_workflow_engine()
contract = await engine.initiate_approval(...)
# ... do other work ...
completed = await engine.wait_for_approval(contract.approval_id)
```

### Workflow Sequence

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         AGENT APPROVAL WORKFLOW                             │
└─────────────────────────────────────────────────────────────────────────────┘

1. DETECTION
   └─> Agent detects Agents task (pattern match: "Set up a Agents pipeline...")

2. INITIATION
   └─> Agent creates ApprovalContract with unique ID
   └─> Contract stored in CosmosDB (audit trail)

3. TEAMS NOTIFICATION
   └─> Adaptive Card sent to approvers via Teams
   └─> Card includes: environment, cluster, image tags, commit SHA, rollback link

4. HUMAN DECISION
   └─> Approver reviews in Teams
   └─> Approver clicks "Approve" or "Reject" (with optional comment)

5. AGENT VALIDATION
   └─> Agent receives decision via webhook/polling
   └─> Validates decision schema (decision present, approver recorded, timestamp set)
   └─> Sets agent_validation = "passed" or "failed"

6. OUTCOME RECORDING
   └─> Final state stored in CosmosDB
   └─> Completion callback triggered

7. EXECUTION CONTROL
   └─> If approved + validated: execution continues
   └─> If rejected OR validation failed: execution blocked
```

### Approval States

| State | Description |
|-------|-------------|
| `pending` | Awaiting human decision |
| `approved` | Human approved in Teams |
| `rejected` | Human rejected in Teams |
| `timeout` | Approval timed out (default: 2 hours) |
| `error` | Workflow error occurred |

### Agent Validation States

| State | Description |
|-------|-------------|
| `pending` | Not yet validated |
| `passed` | Human decision validated by agent |
| `failed` | Validation failed (missing data, schema error) |

---

## Teams Human Approval

### Adaptive Card for Approval Request

Location: [agent365/teams/agents_approval_card.json](teams/agents_approval_card.json)

The approval card includes:

- **Header**: "Agents Deployment Approval Required" with agent identifier
- **Deployment Details**: Environment, cluster, namespace, image tags, commit SHA
- **Task Description**: Full task text
- **Quick Links**: Pipeline URL, Cluster URL, Rollback URL
- **Decision Comment**: Optional text input
- **Actions**: Approve (positive style), Reject (destructive style)

### Approval Card Actions

```json
{
  "type": "Action.Execute",
  "title": "✅ Approve",
  "verb": "approveDeployment",
  "data": {
    "action": "approve",
    "approval_id": "${approval_id}",
    "environment": "${environment}",
    "cluster": "${cluster}"
  }
}
```

### Approval Result Card

Location: [agent365/teams/approval_result_card.json](teams/approval_result_card.json)

Displays final decision with:
- Decision status (approved/rejected with styling)
- Approver identity
- Agent validation status
- Audit information (approval ID, resolution time)

---

## Code Integration

### File: [src/next_best_action_agent.py](../src/next_best_action_agent.py)

### Function: `next_best_action_tool()`

### Location: Lines 1044-1115 (approval checkpoint)

### Task Pattern (Exact Match):
```
"Set up a Agents pipeline for deploying microservices to Kubernetes"
```

### Integration Code

```python
# ================================================================
# AGENT 365 Agents APPROVAL CHECKPOINT
# For Agents pipeline tasks, require human-in-the-loop approval
# via Microsoft Teams before proceeding with plan generation.
# ================================================================
approval_result = None
agents_approval_required = False
CICD_TASK_PATTERN = "Set up a Agents pipeline for deploying microservices to Kubernetes"

if AGENT365_APPROVAL_AVAILABLE and CICD_TASK_PATTERN.lower() in task.lower():
    agents_approval_required = True
    # ... approval workflow execution ...
```

### Response Schema (with approval)

```json
{
  "task_id": "...",
  "task": "Set up a Agents pipeline...",
  "status": "approval_pending | approval_rejected | success",
  "approval_contract": {
    "approval_id": "...",
    "requested_by": "...",
    "task": "...",
    "environment": "...",
    "decision": "approved | rejected | pending",
    "approved_by": "...",
    "timestamp": "...",
    "agent_validation": "passed | failed | pending"
  },
  "metadata": {
    "agents_approval_required": true,
    "approval_result": { ... }
  }
}
```

---

## Fallback Implementation

When Agent 365 is not available, the system falls back to:

### 1. Azure Logic Apps Workflow

Location: [agent365/workflows/agents_approval_logic_app.json](workflows/agents_approval_logic_app.json)

The Logic App:
- Receives approval requests via HTTP trigger
- Logs to CosmosDB
- Sends approval to Teams via Power Automate connector
- Waits for response
- Validates and records decision
- Calls back to agent with result

### 2. Custom Approval Agent

The `ApprovalWorkflowEngine` class serves as the custom agent when Agent 365 is unavailable:

```python
# Fallback triggers Logic App webhook
if self.logic_app_webhook_url:
    await self.teams_client._trigger_logic_app_approval(
        contract, approvers, self.logic_app_webhook_url
    )
```

### Environment Variables for Fallback

```bash
# Logic App webhook URL for approval workflow
LOGIC_APP_APPROVAL_WEBHOOK=https://prod-xx.westus.logic.azure.com/...

# CosmosDB for audit logging
COSMOSDB_ENDPOINT=https://cosmos-xxx.documents.azure.com

# Deployment context
DEPLOYMENT_ENVIRONMENT=production
AKS_CLUSTER_NAME=aks-mcp-cluster
K8S_NAMESPACE=mcp-agents
```

---

## Approval Contract Schema

### Required Contract

```json
{
  "approval_id": "uuid-v4",
  "requested_by": "user@domain.com | service-principal-id",
  "task": "Set up a Agents pipeline for deploying microservices to Kubernetes",
  "environment": "development | staging | production",
  "decision": "approved | rejected | pending | timeout",
  "approved_by": "approver@domain.com",
  "timestamp": "2026-02-01T12:00:00Z",
  "agent_validation": "passed | failed | pending"
}
```

### Extended Contract (Full Audit)

```json
{
  "approval_id": "...",
  "requested_by": "...",
  "task": "...",
  "environment": "...",
  "decision": "...",
  "approved_by": "...",
  "timestamp": "...",
  "agent_validation": "...",
  "cluster": "aks-prod-cluster",
  "namespace": "mcp-agents",
  "image_tags": ["v1.2.3", "latest"],
  "commit_sha": "abc123def456",
  "pipeline_url": "https://dev.azure.com/...",
  "rollback_url": "https://...",
  "comment": "Approved for production release",
  "request_timestamp": "2026-02-01T11:55:00Z",
  "resolution_time_seconds": 300.5
}
```

---

## Deployment Instructions

### Prerequisites

1. Azure subscription with:
   - CosmosDB account
   - Azure Logic Apps (for fallback)
   - AKS cluster

2. Microsoft 365 tenant with:
   - Teams
   - Power Automate (for Teams Approvals connector)
   - Optionally: Frontier preview enrollment

### Step 1: Deploy CosmosDB Container

```bash
# Create approvals container
az cosmosdb sql container create \
  --account-name $COSMOS_ACCOUNT \
  --database-name mcpdb \
  --name approvals \
  --partition-key-path /environment
```

### Step 2: Deploy Logic App (Fallback)

```bash
# Deploy Logic App from workflow definition
az logic workflow create \
  --resource-group $RESOURCE_GROUP \
  --name agents-approval-workflow \
  --definition @agent365/workflows/agents_approval_logic_app.json
```

### Step 3: Configure Environment Variables

```bash
# Add to .env or Kubernetes secrets
COSMOSDB_ENDPOINT=https://cosmos-xxx.documents.azure.com
LOGIC_APP_APPROVAL_WEBHOOK=https://prod-xx.westus.logic.azure.com/...
DEPLOYMENT_ENVIRONMENT=staging
AKS_CLUSTER_NAME=aks-mcp-cluster
K8S_NAMESPACE=mcp-agents
```

### Step 4: Register Agent (If Agent 365 Available)

```bash
# Use the Python SDK
python -c "
import asyncio
from agent365_approval import EntraAgentRegistryClient

async def register():
    client = EntraAgentRegistryClient()
    result = await client.register_agent_instance(
        agent_id='next-best-action-agent-001',
        display_name='Next Best Action Agent',
        description='Agents approval workflow agent',
        url='https://your-agent-endpoint.com'
    )
    print(result)

asyncio.run(register())
"
```

---

## Testing

### Test the Approval Workflow

```python
import asyncio
from agent365_approval import (
    Agent365AvailabilityChecker,
    ApprovalWorkflowEngine,
    ApprovalContract,
)

async def test_approval():
    # 1. Check Agent 365 availability
    checker = Agent365AvailabilityChecker()
    availability = await checker.check_availability()
    print(f"Agent 365 available: {availability.available}")
    
    # 2. Test approval workflow
    engine = ApprovalWorkflowEngine(
        cosmos_endpoint="https://your-cosmos.documents.azure.com",
        logic_app_webhook_url="https://your-logic-app.azurewebsites.net"
    )
    
    contract = await engine.initiate_approval(
        task="Set up a Agents pipeline for deploying microservices to Kubernetes",
        requested_by="test@contoso.com",
        environment="staging",
        cluster="aks-test-cluster"
    )
    
    print(f"Approval initiated: {contract.approval_id}")
    print(f"Status: {contract.decision}")
    
    # 3. Simulate approval response
    completed = await engine.process_approval_response(
        approval_id=contract.approval_id,
        decision="approved",
        approved_by="approver@contoso.com",
        comment="Approved for testing"
    )
    
    print(f"Final decision: {completed.decision}")
    print(f"Agent validation: {completed.agent_validation}")

asyncio.run(test_approval())
```

### Integration Test with MCP Server

```bash
# Run the MCP server
cd src
python -m uvicorn next_best_action_agent:app --host 0.0.0.0 --port 8080

# Test Agents task (in another terminal)
cd tests
python test_next_best_action.py
```

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Agents APPROVAL ARCHITECTURE                        │
└──────────────────────────────────────────────────────────────────────────────┘

                              ┌─────────────────┐
                              │   Developer     │
                              │   (Requester)   │
                              └────────┬────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Next Best Action Agent (MCP)                         │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐           │
│  │ Task Detection  │ → │ Approval Check  │ → │ Plan Generation │           │
│  │ (Agents pattern) │   │ (HITL required) │   │ (post-approval) │           │
│  └─────────────────┘   └────────┬────────┘   └─────────────────┘           │
└─────────────────────────────────┼───────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Approval Workflow Engine                              │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐           │
│  │ Create Contract │ → │ Send to Teams   │ → │ Wait & Validate │           │
│  │ (CosmosDB)      │   │ (Adaptive Card) │   │ (Agent logic)   │           │
│  └─────────────────┘   └────────┬────────┘   └────────┬────────┘           │
└─────────────────────────────────┼──────────────────────┼────────────────────┘
                                  │                      │
          ┌───────────────────────┘                      │
          ▼                                              │
┌─────────────────────┐                                  │
│  Microsoft Teams    │                                  │
│  ┌───────────────┐  │                                  │
│  │ Approval Card │  │         ┌────────────────────────┘
│  │ ✅ Approve    │──┼─────────┤
│  │ ❌ Reject     │  │         │
│  └───────────────┘  │         ▼
└─────────────────────┘   ┌─────────────────┐
                          │   CosmosDB      │
                          │ (Audit Log)     │
                          └─────────────────┘
```

---

## References

### Microsoft Documentation

- [Microsoft Entra Agent Registry](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/what-is-agent-registry)
- [Register Agents to Agent Registry](https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/publish-agents-to-registry)
- [Agent 365 Capabilities](https://learn.microsoft.com/en-us/microsoft-agent-365/admin/capabilities-entra)
- [Teams Approvals API](https://learn.microsoft.com/en-us/graph/approvals-app-api)
- [Adaptive Cards for Teams](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/universal-actions-for-adaptive-cards/up-to-date-views)
- [Azure Logic Apps Webhooks](https://learn.microsoft.com/en-us/azure/connectors/connectors-native-webhook)

### Frontier Preview

- [Microsoft Frontier Program](https://adoption.microsoft.com/copilot/frontier-program/)

---
