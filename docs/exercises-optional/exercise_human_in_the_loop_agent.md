> [!CAUTION]
> These exercises have not been tested. There could be incorrect or missing instructions.

# Optional Exercise: Human-in-the-Loop Agent Approvals

> The current implementation uses Logic Apps and Teams Workflows, not an Agent
> 365 approval API. Use [../../agent-approvals/README.md](../../agent-approvals/README.md)
> for the Next Best Action contract. The illustrative execution examples below
> are not the live recommendation-only authorization contract.

**Duration:** 1 hour

## Overview

In this exercise, you will build a Human-in-the-Loop agent that requires administrator approval via Agent 365 for critical actions. This builds on the Autonomous Agent from [Exercise 2](../exercises/exercise_02_build_agents.md).

---

## Step 1: Create Agent Specification

In VS Code Explorer, navigate to and open `.speckit/specifications/approval_agent.spec.md`.

**Prompt Copilot with:**
> "Create an MCP agent specification for an agent that requires administrator approval via Agent 365 for high-value actions like deployments or data deletions. Governance Model should be Semi-Autonomous."

Key differences from autonomous agent:
- Governance Model: Semi-Autonomous
- Approval workflow integration
- Risk classification for actions

## Step 2: Implement Approval Agent with Copilot

In VS Code Explorer, navigate to and open `src/approval_agent.py`.

**Prompt Copilot with:**
> "Implement an MCP agent that integrates with Agent 365 for human approval. Include a requires_approval() function that classifies actions by risk level, and a request_approval() function that sends approval requests to a Logic App endpoint."

Example approval logic:

```python
def requires_approval(action: str, params: dict) -> bool:
    """Determine if action requires human approval."""
    HIGH_RISK_ACTIONS = [
        "deploy_production",
        "delete_data",
        "modify_permissions",
        "update_configuration"
    ]
    HIGH_VALUE_THRESHOLD = 10000
    
    return (
        action in HIGH_RISK_ACTIONS or
        params.get("value", 0) > HIGH_VALUE_THRESHOLD
    )

async def execute_with_approval(action: str, params: dict) -> dict:
    """Execute action with approval check."""
    if requires_approval(action, params):
        approval = await request_approval(
            agent_id="approval-agent",
            action=action,
            context=params
        )
        if not approval.get("approved"):
            return {"status": "rejected", "reason": approval.get("reason")}
    
    return await execute_action(action, params)
```

## Step 3: Configure Logic App/Teams Integration

Review the custom Next Best Action approval workflow:

In VS Code Explorer, navigate to and open:
- [../../agent-approvals/workflows/agent_approval_logic_app.json](../../agent-approvals/workflows/agent_approval_logic_app.json)
- [../../agent-approvals/teams/agent_approval_card.json](../../agent-approvals/teams/agent_approval_card.json)
- [../../agent-approvals/teams/agent_approval_result_card.json](../../agent-approvals/teams/agent_approval_result_card.json)

## Step 4: Create Unit Tests

In VS Code Explorer, navigate to and open `tests/test_approval_agent.py`.

**Prompt Copilot with:**
> "Create pytest tests for the approval agent including tests for requires_approval() classification, request_approval() integration, and end-to-end approval flow with mock responses."

## Step 5: Run Unit Tests

```powershell
pytest tests/test_approval_agent.py -v
```

## Step 6: Deploy Approval Agent

```powershell
# Build and push
cd src
docker build -t approval-agent:latest .
docker tag approval-agent:latest "$REGISTRY/approval-agent:latest"
docker push "$REGISTRY/approval-agent:latest"

# Deploy
kubectl apply -f k8s/approval-agent-deployment.yaml

# Verify
kubectl get pods -n mcp-agents -l app=approval-agent
```

## Step 7: Functional Test Approval Flow

```powershell
# Port forward
kubectl port-forward -n mcp-agents svc/approval-agent 8081:80

# Test action requiring approval
$body = @{
    jsonrpc = "2.0"
    id = 1
    method = "tools/call"
    params = @{
        name = "deploy_production"
        arguments = @{
            environment = "production"
            version = "v2.0.0"
        }
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://localhost:8081/message" -Method Post -Body $body -ContentType "application/json"
```

**Expected Behavior:**
1. Agent identifies action as high-risk
2. Approval request sent to Logic App
3. Teams adaptive card posted to approval channel
4. Agent waits for approval response
5. Action proceeds or is rejected based on response

---

## Step 8: Review Agent Approvals

The custom Logic App/Teams workflow provides human review; Python remains the authorization authority.

### Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Your Agent                                   │
│                 Identifies high-value decision                        │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  Logic App/Teams Approval Flow                        │
│           Logic App → Teams Adaptive Card → Human Response            │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                Approval Decision Recorded                             │
│                     Cosmos DB Audit Log                               │
└──────────────────────────────────────────────────────────────────────┘
```

### Verify Logic App Execution

1. Open Azure Portal
2. Navigate to **Logic Apps** → **agent-approval-workflow**
3. Go to **Run history**
4. Review successful and failed runs

**For each run, verify:**
- Trigger received the approval request payload
- Teams adaptive card was posted successfully
- Approval response was received
- Callback to agent completed

### Check Teams Channel

1. Navigate to Microsoft Teams
2. Find the Agents Approval channel
3. Review recent approval cards:
   - Agent ID and action requested
   - Risk level classification
   - Context summary
   - Approve/Reject buttons

**Verify adaptive card contains:**
- Clear description of the action
- Risk level (high/medium/low)
- Sufficient context for decision-making
- Response buttons functioning

### Review Approval History in Cosmos DB

1. Navigate to Cosmos DB → **approvals** container
2. Query recent approvals:

```sql
SELECT * FROM c 
WHERE c.agent_id = 'approval-agent' 
ORDER BY c.timestamp DESC 
OFFSET 0 LIMIT 10
```

**Verify approval document structure:**

```json
{
  "id": "approval-request-id",
  "agent_id": "approval-agent",
  "action": "deploy_production",
  "approved": true,
  "approver": "user@contoso.com",
  "context": { "environment": "production", "version": "v2.0.0" },
  "timestamp": "2026-02-07T10:30:00Z"
}
```

### Generate Compliance Report

Query approval metrics in Application Insights:

```kusto
// Approval request metrics
customEvents
| where name == "ApprovalRequest"
| where timestamp > ago(7d)
| summarize 
    TotalRequests = count(),
    Approved = countif(customDimensions["approved"] == "true"),
    Rejected = countif(customDimensions["approved"] == "false"),
    AvgResponseTime = avg(todouble(customDimensions["response_time_seconds"]))
| extend ApprovalRate = round(Approved * 100.0 / TotalRequests, 2)
```

### Approval Best Practices

| Practice | Description |
|----------|-------------|
| **Set appropriate timeouts** | Don't block indefinitely; define escalation paths |
| **Provide rich context** | Include enough info for informed decisions |
| **Log everything** | Record all requests and decisions for audit |
| **Handle rejection gracefully** | Provide clear feedback when actions are rejected |
| **Define escalation paths** | What happens when approval times out? |

---

## Verification Checklist

- [ ] Specification created with Semi-Autonomous governance
- [ ] Agent implemented with approval workflow
- [ ] requires_approval() correctly classifies actions
- [ ] Agent 365 integration configured
- [ ] Unit tests passing
- [ ] Agent deployed to AKS and running
- [ ] Approval flow working end-to-end
- [ ] Logic App execution verified
- [ ] Teams adaptive cards functioning
- [ ] Approval history recorded in Cosmos DB

---

**Back to:** [Exercise 2: Build Agents](../exercises/exercise_02_build_agents.md)
