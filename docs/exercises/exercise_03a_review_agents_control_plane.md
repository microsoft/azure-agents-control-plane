# Exercise 3A: Review Agents Control Plane, Agent ID, and Agent 365

**Duration:** 45 minutes

## Overview

In this exercise, you are to inspect the complete Azure Agents Control Plane to understand how identity, security, governance, memory, and observability works for your new agent. You will also review the agent's Microsoft Entra Agent ID and the Agent 365 functionality implemented by this accelerator. You will be asked to answer some questions about the implementation.

---

## Step 3.1: Check APIM – Policies (Security/Management/Governance)

Azure API Management enforces governance policies for all agent traffic. In this accelerator, APIM serves as the centralized gateway that fronts all MCP tool calls and agent-to-agent communication, enforcing OAuth authentication, request routing to the AKS-hosted MCP server, rate limiting, and policy-based security so that every interaction between agents and tools is authenticated, authorized, and observable.

### Navigate to APIM

1. Open Azure Portal
2. Navigate to **API Management** → Your APIM instance
3. Go to **APIs** → **MCP API** → **Design** → **Inbound processing**

### View the policies

| Question | Copilot/Your Answers |
|----------|-------------|
| How does the policy on all operations enforce authentication? | |
| What happens if an unauthenticated request is made to an endpoint in this APIM? | |
| Can you think of other policies that should be added to govern, manage and security APIs? | |

---

## Step 3.2: Check Cosmos DB (Short-Term Memory)

Cosmos DB stores plans and tasks. In this accelerator, Cosmos DB serves as the agent's short-term memory provider, storing session-scoped planning artifacts—including intent decomposition, multi-step task plans, and individual task execution state—with TTL-based automatic expiration so that ephemeral reasoning data is cleaned up after the session ends. Each memory entry is partitioned by session ID and includes vector embeddings, enabling the agent to perform cosine-similarity searches over recent context and retrieve relevant conversation history or prior plan steps during agentic reasoning.

### Navigate to Cosmos DB

1. Open Azure Portal
2. Navigate to **Azure Cosmos DB** → Your account
3. Go to **Data Explorer**

### View Plans and Tasks

Click on plans and then Items.

Review tasks, intent and steps.

Click on tasks and then Items.

Take note that embeddings have been stored.


---

## Step 3.3: Check Azure AI Foundry / AI Search (Long-Term Memory)

Azure AI Search provides vector search for long-term memory retrieval. In this accelerator, Azure AI Foundry's agentic retrieval pipeline powers the agent's long-term memory by indexing durable knowledge sources and knowledge bases that persist beyond any single session. When the agent needs to recall prior experience or domain knowledge, the pipeline performs source selection and query planning across multiple knowledge sources, ranks results through L2/L3 classifiers, and optionally reflects and iterates before merging final results. The pipeline's **reasoning effort level** (Minimal, Low, or Medium) controls how much computation is applied at each stage—higher levels enable query planning, L3 classification, and reflection/iteration loops for deeper, more accurate retrieval at the cost of additional latency, while lower levels skip those stages for faster responses.

### Navigate to the Knowledge Base

1. Open Azure Portal
2. Navigate to **Azure AI Search** → Your service → **Knowledge bases**
3. Open the **task-instructions-kb** knowledge base

### Query the Knowledge Base

In the knowledge base chat panel, enter a query to test retrieval:

```
What are the steps for customer churn analysis?
```

Review the response and note how the knowledge base retrieves relevant task instructions from the **task-instructions-source** knowledge source.

Try additional queries (and perhaps a question specific to your domain):

```
How do I set up a CI/CD Kubernetes pipeline?
```

```
What is the REST API user management workflow?
```

### Review Knowledge Base Configuration

In the left panel, review the following settings:

| Setting | Expected Value | Purpose |
|---------|---------------|---------|
| Knowledge sources | task-instructions-source | Indexed task instruction documents |
| Chat completion model | (see note below) | Required for reasoning effort above Minimal |
| Reasoning effort | Minimal (default) | Controls depth of retrieval pipeline |
| Retrieval mode | Retrieval (recommended) | Uses agentic retrieval with ranking |

> **Note:** If no **Chat completion model** is configured, the reasoning effort must be set to **Minimal**. To use **Low** or **Medium** reasoning effort (which enables query planning, L3 classification, and reflection/iteration), click **+ Add model deployment** and select a deployed chat completion model (e.g., GPT-4o). Without a model, attempting a higher reasoning effort will produce the error: *"A Knowledge Base model must be specified to use any reasoning effort other than 'Minimal'"*.

---

## Step 3.4: Check Fabric IQ (Facts/Ontology)

Ontologies provide grounded facts for agent reasoning. In agentic systems, an ontology is a structured representation of domain knowledge—defining entity types, relationships, and facts—that the agent uses to anchor its reasoning in verified, real-world data rather than relying solely on the LLM's parametric knowledge. This grounding is critical because it prevents hallucination, ensures consistency across agent sessions, and enables the agent to reason over domain-specific concepts (e.g., "Customer has churn risk 0.85") that the base model was never trained on.

Facts within ontologies serve as the agent's **source of truth**. When the agent retrieves context during planning or task execution, ontology facts provide deterministic, structured data points—such as customer segments, churn predictions with confidence scores, pipeline failure categories, or API endpoint schemas—that complement the probabilistic outputs of the LLM. This combination of structured facts and generative reasoning is what enables agents to produce accurate, actionable recommendations grounded in your organization's actual data.

In this accelerator, ontologies are stored as JSON files and uploaded to a storage account (or Microsoft Fabric OneLake), where the agent can retrieve them at runtime. Three domain ontologies are included:

| Ontology | Domain | Key Facts |
|----------|--------|-----------|
| `customer_churn_ontology.json` | Customer Analytics | Churn risk predictions, segment retention insights, engagement metrics |
| `cicd_pipeline_ontology.json` | DevOps | Pipeline failure categories, deployment events, cluster health |
| `user_management_ontology.json` | API Management | User roles, endpoint schemas, access control patterns |

> **Note:** This step will be fully fleshed out once the Fabric IQ environment is ready. Detailed navigation and query instructions will be added at that time.


---

## Step 3.5: Check Log Analytics (Observability)

Azure Monitor collects logs, metrics, and traces from all agents.

### Navigate to Log Analytics Workspaces

1. Open Azure Portal
2. Navigate to **Log Analytics Workspaces** → Your workspace
3. Go to **Logs**

### Query Agent Logs

> **Note:** This accelerator uses the legacy `ContainerLog` table (v1) rather than `ContainerLogV2`. The v1 table uses `LogEntry` instead of `LogMessage`, and container metadata fields (`Name`, `Image`) may be empty when using the AMA agent with the OMS addon.


```kusto
// Agent container logs (ContainerLog v1)
ContainerLog
| where TimeGenerated < ago(60m)
| where LogEntry !contains "/health"
| project TimeGenerated, LogEntry, LogEntrySource, ContainerID
| order by TimeGenerated desc
| take 100
```

The logs reflect the runtime behavior of `next_best_action_agent.py` — a FastAPI MCP server that initializes CosmosDB clients for task and plan storage, sets up memory providers (short-term via CosmosDB, long-term via AI Search, and facts via Fabric IQ), generates embeddings for semantic similarity search, analyzes user intent, produces action plans, and optionally leverages the Azure Agents Learning SDK for in-process reinforcement learning — along with standard HTTP request handling from the Uvicorn server.

---

## Step 3.6: Check Entra ID / RBAC

### Obtain the Agent Managed Identity Client ID

The agent's managed identity client ID is stored as an annotation on the Kubernetes service account used by the agent pods. This is **not** the same as the OAuth app registration (e.g., `MCP-OAuth-app-*`) visible in Entra ID → App registrations — that app is used by APIM for OAuth token validation.

Retrieve the managed identity client ID from the service account:

```powershell
# Get the managed identity client ID from the service account annotation
kubectl get serviceaccount mcp-agent-sa -n mcp-agents -o jsonpath='{.metadata.annotations.azure\.workload\.identity/client-id}'
```

Save the output — you will use it in the commands below.

### Review Role Assignments

```powershell
# List role assignments for agent identity (replace with your client ID from above)
az role assignment list --assignee <managed-identity-client-id> --all --output table
```

### Expected Roles

| Role | Resource | Purpose |
|------|----------|---------|
| Cognitive Services User | AI Foundry | LLM inference |
| Cosmos DB Data Contributor | Cosmos DB | Read/write sessions |
| Storage Blob Data Reader | Storage Account | Read ontologies |
| Search Index Data Reader | AI Search | Query long-term memory |

### Verify Workload Identity

```powershell
# Check service account annotation
kubectl get serviceaccount mcp-agent-sa -n mcp-agents -o yaml

# Expected annotation:
# azure.workload.identity/client-id: <managed-identity-client-id>
```

> **Identity boundary:** In the minimal Agent 365 integration, this user-assigned managed identity remains the credential for Azure data-plane access. The Entra Agent ID reviewed next is a separate identity used for Agent 365 governance and authenticated observability unless an explicit Azure credential migration has been completed.

---

## Step 3.7: Review Microsoft Entra Agent ID

Microsoft Entra Agent ID gives the autonomous agent its own directory identity and links it to an Agent Identity Blueprint and an accountable human owner or sponsor. Agent ID is separate from the AKS managed identity, the APIM OAuth app registration, and the Agent 365 catalog registration.

> **Review only:** Do not run `a365 setup requirements`, use `-Apply` or `-Deploy`, or make Graph permission changes during this exercise. Those operations can create identities, grant consent, or update the deployed workload. Follow the [Agent 365 onboarding guide](../AGENTS_AGENT365_ONBOARDING.md) when authorized onboarding is required.

### Review the adopted identity settings

Replace `<azd-environment>` with the environment used for the lab:

```powershell
$environment = "<azd-environment>"
$settingNames = @(
    "AGENT_IDENTITY_PROVISIONING_MODE",
    "AGENT_IDENTITY_ENABLED",
    "AGENT_IDENTITY_DISPLAY_NAME",
    "AGENT_IDENTITY_APP_ID",
    "AGENT_IDENTITY_PRINCIPAL_ID",
    "AGENT_IDENTITY_BLUEPRINT_APP_ID",
    "AGENT_IDENTITY_BLUEPRINT_OBJECT_ID"
)

$settingNames | ForEach-Object {
    [pscustomobject]@{
        Setting = $_
        Value = azd env get-value $_ --environment $environment
    }
} | Format-Table -AutoSize
```

After standard onboarding, the identity IDs should be populated and `AGENT_IDENTITY_PROVISIONING_MODE` should normally be `adopt`. For the minimal integration, `AGENT_IDENTITY_ENABLED=false` is intentional: Azure data clients continue to use the existing AKS managed identity while Agent 365 telemetry authenticates as the child Agent ID. A value of `true` is valid only after the child identity has received the required Azure RBAC and token exchange has been tested.

If the identity fields are empty, record Agent ID as **not onboarded**. Do not invent IDs or use a local registration GUID as proof of an Entra identity.

### Review the blueprint and child Agent ID

1. Open the **Microsoft Entra admin center** in the hosting tenant.
2. Go to **Entra ID** → **Agents** → **Agent identities**.
3. Locate the identity whose application ID matches `AGENT_IDENTITY_APP_ID`.
4. Confirm its display name, object ID, blueprint relationship, owner, and sponsor.
5. Go to **Agent blueprints** and locate the blueprint whose application ID matches `AGENT_IDENTITY_BLUEPRINT_APP_ID`.
6. Confirm the blueprint application object ID matches `AGENT_IDENTITY_BLUEPRINT_OBJECT_ID` and that the intended human is an owner.

### Verify secretless federation

The standard onboarding flow adds a federated credential to the blueprint so the existing AKS managed identity can bootstrap Agent ID authentication without a client secret. Review the blueprint's federated credentials and verify:

| Property | Expected Value |
|----------|----------------|
| Issuer | `https://login.microsoftonline.com/<hosting-tenant-id>/v2.0` |
| Subject | Existing AKS managed identity **principal/object ID**, not its client ID |
| Audience | `api://AzureADTokenExchange` |

Do not print, copy, or commit `a365.generated.config.json`; it can contain protected onboarding state.

### Record the Agent ID review

| Question | Copilot/Your Answers |
|----------|----------------------|
| Does a real child Agent ID exist in the hosting tenant? | |
| Is it linked to the expected blueprint? | |
| Are the blueprint, Agent ID, and catalog owners the intended accountable human? | |
| Does the blueprint federation trust the AKS managed identity principal ID? | |
| Is `AGENT_IDENTITY_ENABLED` consistent with the Azure credential strategy? | |

---

## Step 3.8: Review Agent 365 Functionality

Agent 365 provides registration, ownership, lifecycle governance, and authenticated observability for this externally hosted agent. It does **not** replace AKS hosting or automatically authorize runtime actions. In this accelerator, human approval remains a custom, durable Logic App/Teams workflow rather than a native Agent 365 approval API.

### Review the Agent 365 catalog registration

1. Open the **Microsoft 365 admin center** in the same hosting tenant as the Agent ID.
2. Go to **Agents** → **All agents** → **Registry**.
3. Locate the registered agent by name.
4. Confirm the registration is bound to the child Agent ID reviewed in Step 3.7.
5. Confirm the catalog owner is the intended accountable human.

A real catalog record is required. A generated manifest, inventory package ID, or locally stored GUID does not prove that registration completed.

### Review Agent 365 observability

Check the configured mode:

```powershell
azd env get-value AGENT_OBSERVABILITY_MODE --environment <azd-environment>
```

| Mode | Expected Behavior |
|------|-------------------|
| `off` | No Agent 365 SDK initialization or export |
| `console` | Local metadata spans only; no Agent 365 token or network export |
| `agent365` | Authenticated S2S export under the child Agent ID |

Before `agent365` mode is considered functional, verify all of the following:

- The Agent ID and catalog registration are real and owned.
- The secretless blueprint-to-managed-identity federation is present.
- The exact `Agent365.Observability.OtelWrite` **application** permission has been granted for S2S export.
- At least one tenant user has an assigned eligible Microsoft 365 E7 or Microsoft Agent 365 license.
- A non-sensitive test invocation produces accepted spans visible under the registered agent's **Activity** view.

An HTTP 200 response alone does not prove telemetry ingestion. Check both rejected-span results and downstream Activity visibility. The implementation exports metadata such as operation name, duration, status, tool name, token counts, and approval state; it must not export prompts, responses, tool arguments, user identity, signed URLs, or raw exception details.

### Review human approval functionality

The existing approval path is:

1. The agent creates a durable pending approval record in Cosmos DB.
2. A Logic App posts an adaptive card to the private **Agent Approvals** Teams channel.
3. A human approves or rejects the exact request.
4. The Logic App sends an authenticated callback.
5. The agent validates the matching, unexpired decision before continuing.

When a deployed test endpoint and a human approver are available, follow the [online approval validation](../AGENTS_APPROVAL_FLOW_VALIDATION.md#repeat-the-online-test-intentionally) with a new request. This test proves the custom Logic App/Teams authorization path; it does not prove a native Agent 365 approval capability.

### Record the Agent 365 review

| Capability | Observed State | Evidence / Notes |
|------------|----------------|------------------|
| Catalog registration and owner | Configured / Not configured | |
| Registration bound to child Agent ID | Pass / Fail / Not tested | |
| Agent 365 S2S observability | Off / Console only / Export verified | |
| Metadata-only telemetry controls | Pass / Fail / Not tested | |
| Logic App/Teams human approval | Pass / Fail / Not tested | |
| Agent 365 mailbox, Teams bot, or Work IQ | Not used | |

For implementation details and current environment limitations, review [Agent 365 onboarding](../AGENTS_AGENT365_ONBOARDING.md) and [Agent 365 usage analysis](../AGENTS_AGENT365_USAGE_ANALYSIS.md).

---

## Step 3.9: Identify Problems

Based on your review, identify any issues with your agents:

### Checklist

| Component | Status | Issue Found | Notes |
|-----------|--------|-------------|-------|
| APIM Policies | ✅ / ❌ | | |
| Short Term Memory (CosmosDB Plans/Tasks) | ✅ / ❌ | | |
| Long Term Memory (FoundryIQ Instructions) | ✅ / ❌ | | |
| Facts (Fabric IQ Ontologies) | ✅ / ❌ | | |
| Log Analytics | ✅ / ❌ | | |
| Entra ID + RBAC | ✅ / ❌ | | |
| Entra Agent ID + Blueprint | ✅ / ❌ / Not onboarded | | |
| Agent 365 Registration + Ownership | ✅ / ❌ / Not onboarded | | |
| Agent 365 Observability | ✅ / ❌ / Not enabled | | |
| Logic App/Teams Approval | ✅ / ❌ / Not tested | | |

### Common Problems

| Problem | Symptom | Solution |
|---------|---------|----------|
| High latency | P95 > 2s | Check AI Foundry throttling, scale pods |
| Failed tool calls | Error rate > 5% | Review logs for exceptions |
| Missing traces | No transactions in App Insights | Verify OpenTelemetry configuration |
| RBAC errors | 403 responses | Add missing role assignments |
| Agent ID not onboarded | Identity or blueprint IDs are empty | Complete authorized standard Agent 365 onboarding or adopt an approved identity |
| Invalid Agent ID federation | S2S token exchange fails | Verify the hosting tenant, UAMI principal ID, issuer, and token-exchange audience |
| Agent missing from Registry | Local IDs exist but no catalog record is visible | Verify standard onboarding completed and retained its CLI deployment state |
| Agent 365 telemetry missing | Invocation succeeds but no Activity appears | Verify mode, application permission, assigned license, rejected spans, and rollout |
| Approval is bypassed or stale | Protected request proceeds without a current matching decision | Keep the Logic App approval path enabled and repeat the online approval test |

---

## Completion Checklist

Before proceeding to Exercise 4, please confirm the following:

- [ ] Reviewed APIM policies and understand security, manageability and governance controls
- [ ] Verified Cosmos DB is storing plans and tasks
- [ ] Confirmed FoundryIQ (AI Search) has agentic retrieval of instructions
- [ ] Reviewed ontology files in storage account / Fabric IQ
- [ ] Queried Log Analytics for agent logs
- [ ] Verified Entra ID + RBAC role assignments
- [ ] Distinguished the AKS managed identity, APIM OAuth app, Entra Agent ID, blueprint, and Agent 365 registration
- [ ] Verified Agent ID ownership, blueprint relationship, and secretless federation, or recorded that onboarding is incomplete
- [ ] Reviewed Agent 365 registration and observability evidence without treating HTTP success as proof of ingestion
- [ ] Verified the Logic App/Teams approval boundary or recorded that a live approval test remains outstanding
- [ ] Documented any problems found

---

**Next:** [Exercise 4: Optimize and Evaluate Agent](exercise_04_optimize_and_evaluate_agent.md)