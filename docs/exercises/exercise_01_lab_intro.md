# Exercise 1: Lab Intro

**Duration:** 30 minutes

## Overview

This exercise reviews the solution architecture, introduces the lab, and validates that your environment is properly configured.

---

## Step 1.1: Review Lab Objectives

By completing this lab, you will learn how to:

### Build Your Agent
- ✅ Review the SpecKit constitution and understand the SDLC principles in play
- ✅ Write a detailed specification for your agent by following the SpecKit methodology
- ✅ Integrate with the Azure Agents Control Plane infrastructure as follows:
  - ✅ Implement an MCP-compliant agent using FastAPI
  - ✅ Containerize your agent with Docker
  - ✅ Deploy your agent to AKS as a new pod

### Enterprise Governance
- ✅ Understand how Azure API Management acts as the governance gateway
- ✅ Inspect APIM policies that enforce rate limits, quotas, and compliance with enterprise standards
- ✅ Trace requests through the control plane using distributed tracing

### Security & Identity
- ✅ Configure identity for agents running in AKS pods
- ✅ Implement least-privilege RBAC for agent resources and tooling access
- ✅ Validate keyless authentication patterns

### Observability
- ✅ Monitor agent behavior using Azure Monitor and Application Insights
- ✅ Query telemetry with Kusto Query Language (KQL)

### Learning & Evaluation
- ✅ Capture agent episodes for training data collection
- ✅ Produce rewards from Azure AI Evaluation judges or manual labels
- ✅ Optimize agent behavior with the Azure Agents Learning SDK (in-process reinforcement learning)
- ✅ Run structured evaluations measuring intent resolution, tool accuracy, and task adherence

---

## Step 1.2: Review Solution Architecture

### Azure Agents Control Plane

The Azure Agents Control Plane provides centralized security, governance, and observability for enterprise AI agents. The following diagram depicts the runtime architecture:

![Agent Software Development Lifecycle](../../runtime.svg)

### Architecture Components

The control plane is composed of Azure services that each fulfill a distinct role — from API governance and identity to memory, observability, and human oversight. Together, they form a layered architecture where every agent request is authenticated, authorized, planned, actioned, logged, and traceable.

| Component | Azure Service | Purpose |
|-----------|---------------|---------|
| API Gateway | Azure API Management | Governance, rate limiting, OAuth validation |
| Agent Runtime | Azure Kubernetes Service | Workload identity, container orchestration |
| Short-Term Memory | Azure Cosmos DB | Session context, episode storage |
| Long-Term Memory | Azure AI Search | Historical data, vector search |
| Facts/Ontology | Microsoft Fabric / Storage Account | Domain knowledge, grounded facts |
| AI Models | Azure AI Foundry | LLM inference, embeddings |
| Identity | Microsoft Entra ID | Agent identity, RBAC |
| Observability | Azure Monitor + App Insights | Metrics, traces, logs |
| Human Oversight | Agent 365 | Approval workflows, Teams integration |

### SpecKit Methodology

SpecKit is a **specification-driven development methodology** that ensures all agents are properly defined before implementation. Rather than jumping straight into code, SpecKit requires you to first articulate what your agent does, what tools it exposes, its governance model, and its expected behavior — all in a structured specification document.

The project is organized around two key artifacts:

- **Constitution** — The governance framework that applies to all agents in the project. It defines shared principles such as security posture, naming conventions, MCP compliance requirements, and approval policies. Every agent specification must conform to the constitution.
- **Specifications** — Individual agent definitions that describe the agent's domain, tools, input/output schemas, risk levels, and test criteria. In Exercise 2, you will write your own specification before generating any implementation code.

```
.speckit/
├── constitution.md           # Core principles and governance framework
└── specifications/           # Individual agent specifications
    ├── customer_churn_agent.spec.md
    ├── devops_cicd_pipeline_agent.spec.md
    └── your_agent.spec.md    # Your agent specification (Exercise 2)
```

The SpecKit workflow follows this sequence:

1. **Review the constitution** to understand the rules your agent must follow
2. **Write a specification** describing your agent's purpose, tools, and governance model
3. **Use GitHub Copilot** to generate implementation code from the specification
4. **Validate** that the implementation matches the specification through testing

You will work hands-on with SpecKit in [Exercise 2: Build Agents](exercise_02_build_agents.md).

---

## Step 1.3: Validate Environment

Use **GitHub Copilot in Agent Mode** to complete each validation step below. Open Copilot Chat (`Ctrl+Alt+I`), select **Agent** mode from the dropdown, and enter the prompts listed for each step. Copilot will run the commands in VS Code's integrated terminal and help you resolve any issues.

### Prerequisites Check

Prompt Copilot:

> *"Check that I have the prerequisites installed: Python 3.11+, Docker, Azure CLI (az and azd), and kubectl. Also verify I'm logged in to Azure."*

Copilot will run the version checks and report any missing tools.

### AKS Cluster Access

Prompt Copilot:

> *"Verify I can connect to the AKS cluster and that the mcp-agents namespace exists."*

Copilot will run `kubectl get nodes` and `kubectl get namespace mcp-agents`. If the connection fails, ask Copilot: *"Help me get AKS credentials for my cluster."*

### Environment Variables

Prompt Copilot:

> *"Check that the required environment variables are set: CONTAINER_REGISTRY, AZURE_TENANT_ID, FOUNDRY_PROJECT_ENDPOINT, and COSMOSDB_ENDPOINT."*

Copilot will inspect your terminal session and identify any missing variables. If values are missing, ask Copilot: *"Help me set the missing environment variables from my Azure deployment."*

### Port-Forward Tunnel to MCP Agents

Prompt Copilot:

> *"Open a port-forward tunnel to the mcp-agents service in AKS on port 8000."*

Copilot will run `kubectl port-forward -n mcp-agents svc/mcp-agents 8000:80` in a background terminal. While the tunnel is active, agent endpoints are available at `http://localhost:8000`. Use a separate terminal for running tests.

### Execute Environment Validation Test

Prompt Copilot:

> *"Activate the .venv virtual environment and run tests/test_next_best_action_functionals.py in direct mode."*

Copilot will activate the virtual environment, navigate to the tests directory, and execute the connection test.

**Expected Output:**

```
======================================================================
🧪 Testing next_best_action MCP Tool
======================================================================
✅ Loaded configuration from tests/mcp_test_config.json
🔗 Direct Mode URL: http://localhost:8000/runtime/webhooks/mcp

📡 Establishing SSE session...
✅ Got session URL

📋 Listing available tools...
✅ Found 36 tools
✅ Memory tools available - short-term memory is enabled

======================================================================
🤖 Testing next_best_action with sample tasks
======================================================================

🎯 Test 1/3 — Analyze customer churn data...
✅ Task analysis complete (plan generated, stored in Cosmos)

🎯 Test 2/3 — Set up a CI/CD pipeline...
✅ Task analysis complete (plan generated, stored in Cosmos)

🎯 Test 3/3 — Design a REST API...
✅ Task analysis complete (plan generated, stored in Cosmos)
```

### Troubleshooting

| Issue | Solution |
|-------|----------|
| AKS connection failed | Run: `az aks get-credentials --resource-group <rg> --name <cluster>` |
| Namespace not found | Deploy base infrastructure: `azd provision` |
| Pods not running | Check logs: `kubectl logs -n mcp-agents -l app=mcp-server` |
| Pod not starting | Check logs: `kubectl logs -n mcp-agents -l app=my-agent` |
| Health endpoint failing | Verify service: `kubectl get svc -n mcp-agents` |
| Health check failing | Ensure `/health` endpoint returns 200 |
| Image pull errors | Verify ACR login: `az acr login --name <registry>` |
| Workload identity issues | Verify service account annotations |

---

## Completion Checklist

Before proceeding to Exercise 2, please confirm the following:

- [ ] Reviewed lab objectives and understand what you will build
- [ ] Reviewed solution architecture and key Azure services
- [ ] Environment validation script executed successfully
- [ ] All required tools installed (Python, Docker, Azure CLI, kubectl)
- [ ] Connected to AKS cluster
- [ ] Environment variables configured

---

**Next:** [Exercise 2: Build Agents using GitHub Copilot and SpecKit](exercise_02_build_agents.md)
