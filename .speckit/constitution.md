# Azure Agents Control Plane Constitution

## Project Overview

The Azure Agents Control Plane governs the complete lifecycle of enterprise AI agents, SDLC: analysis, design, development, testing, learning, and evaluation. This constitution establishes the principles, standards, and governance framework for building agents using Azure API Management (APIM), Model Context Protocol (MCP), and Azure Kubernetes Service (AKS) as the foundational control plane infrastructure.

**Mission**: Enable enterprise-grade AI agent development where Azure provides centralized governance, observability, identity, and compliance—regardless of agent execution location.

## Core Principles

### 1. Azure as Enterprise Control Plane

- **Centralized Governance**: All agent operations flow through Azure's control plane (APIM + MCP + AKS)
- **Multi-Cloud Capable**: Agents may execute on Azure, GCP, AWS, or hybrid environments
- **Policy Enforcement**: Consistent policies applied at the gateway layer before any model/agent execution
- **Single Pane of Glass**: Azure Monitor, App Insights, and Sentinel provide unified observability

### 2. API-First Agent Architecture

- **APIM Gateway**: All agent tool calls route through Azure API Management
- **MCP Protocol**: Model Context Protocol standardizes tool interfaces across agents
- **OpenAPI Compliance**: All tools and services expose OpenAPI specifications
- **Rate Limiting & Quotas**: APIM policies govern resource consumption per agent/user/tenant

### 3. Identity-First Security

- **Agent Identities**: Every agent receives a Microsoft Entra ID identity (Agent ID)
- **Workload Identity**: AKS pods authenticate via federated workload identity
- **Keyless Authentication**: Prefer managed identities; vault secrets only when unavoidable
- **Least Privilege**: RBAC scopes permissions to specific tools, actions, and data

### 4. Specification-Driven Development

- **SpecKit Methodology**: All agents defined via structured specifications before implementation
- **GitHub Copilot Integration**: Copilot accelerates agent development from specifications
- **Test-First Approach**: Acceptance criteria are testable and automated
- **Schema Evolution**: Specifications drive code, not vice versa

### 5. Continuous Evaluation & Improvement

- **Agent Evaluations**: Built-in evaluation framework measures task adherence, safety, and quality
- **Reinforcement Learning**: The Azure Agents Learning SDK captures episodes and learns an action-selection policy in-process
- **Behavioral Optimization**: Continuous feedback loops improve agent performance
- **Human-in-the-Loop**: Agent 365 enables oversight for critical decisions

## Development Standards

### Analysis Phase

- Document business problem and target outcomes
- Identify multi-agent decomposition opportunities
- Map data sources, inputs, and outputs
- Define success KPIs and measurement approach
- Assess compliance and regulatory requirements

### Design Phase

- Create agent architecture diagrams (Mermaid format)
- Define tool catalog with MCP schemas
- Specify memory architecture (short-term, long-term, facts)
- Design identity and access patterns
- Plan observability instrumentation

### Development Phase

#### Code Quality
- Type hints for all functions (Python 3.10+)
- Comprehensive docstrings (Google style)
- Unit tests with >80% coverage for core logic
- Integration tests for end-to-end workflows
- Linting with ruff (pyproject.toml configuration)

#### Dependency Management
- UV as exclusive dependency manager
- Lock file committed for reproducibility
- Minimal direct dependencies (prefer stdlib when practical)
- Clear separation: core dependencies vs dev/test dependencies

#### Agent Implementation
- FastAPI for MCP server endpoints
- Azure SDK for service integration
- OpenTelemetry for distributed tracing
- Pydantic for request/response validation

### Testing Phase

- **Unit Tests**: Isolated agent logic and tool implementations
- **Integration Tests**: End-to-end MCP protocol flows
- **Infrastructure Tests**: AKS connectivity, APIM routing, identity
- **Functional Tests**: Use case validation via Copilot-driven testing
- **Security Tests**: Authentication, authorization, input validation

### Learning Phase

- **Episode Capture**: Enable Azure Agents Learning SDK capture for training data collection
- **Reward Signal**: Score episodes with Azure AI Evaluation judges (intent resolution, task adherence, task completion) or human labels
- **Policy Initialization**: Define the discrete action space (prompt variants, retrieval strategies) the policy selects from
- **Policy Learning**: Run offline REINFORCE-with-baseline batches to update the policy in-process
- **Evaluation**: Compare policy versions and roll improvements forward after validation

### Evaluation Phase

- **Task Adherence**: Measure agent compliance with specified workflows
- **Safety Scoring**: Evaluate content safety and policy compliance
- **Quality Metrics**: Accuracy, latency, token efficiency
- **Business KPIs**: Domain-specific success metrics
- **Regression Testing**: Ensure new versions don't degrade performance

## Technical Architecture

### Module Structure
```
src/
├── agents/              # Agent implementations
│   ├── orchestrator.py  # Multi-agent coordination
│   ├── context_agent.py # Context aggregation
│   ├── scoring_agent.py # Decision scoring
│   └── approval_agent.py # Governance and approval
├── memory/              # Memory providers
│   ├── short_term.py    # CosmosDB session memory
│   ├── long_term.py     # AI Search persistent memory
│   └── facts.py         # Fabric IQ ontology-grounded facts
├── tools/               # MCP tool implementations
│   ├── registry.py      # Tool catalog
│   └── adapters/        # External service adapters
├── learning/            # Azure Agents Learning SDK integration (in-process RL)
│   ├── capture.py       # Episode capture hook
│   ├── policy.py        # Softmax policy over discrete actions
│   └── runner.py        # REINFORCE learning runner
└── evaluation/          # Evaluation framework
    ├── metrics.py       # Metric definitions
    ├── runners.py       # Evaluation execution
    └── reports.py       # Result visualization
```

### Infrastructure Components
```
infra/
├── core/                # Core Azure resources
│   ├── aks.bicep        # Kubernetes cluster
│   ├── apim.bicep       # API Management
│   └── storage.bicep    # Storage accounts
├── ai/                  # AI services
│   ├── foundry.bicep    # Azure AI Foundry
│   └── search.bicep     # Azure AI Search
├── identity/            # Identity resources
│   ├── agent-id.bicep   # Entra Agent Identity
│   └── workload.bicep   # Workload identity
└── observability/       # Monitoring
    ├── monitor.bicep    # Azure Monitor
    └── insights.bicep   # Application Insights
```

### Data Flow
1. **Request Ingestion**: AI client → APIM → OAuth validation
2. **Protocol Handling**: APIM → AKS → MCP Server (SSE/JSON-RPC)
3. **Tool Execution**: Agent → Tool → External Service (via managed identity)
4. **Memory Operations**: Agent → Memory Provider → CosmosDB/AI Search/Fabric
5. **Telemetry**: All operations → OpenTelemetry → App Insights
6. **Learning**: Episodes → Judges → REINFORCE → Policy Update

## Success Criteria

### Functional Requirements
- ✅ Agents authenticate via Entra ID with managed identities
- ✅ All tool calls governed by APIM policies
- ✅ MCP protocol compliance for tool discovery and execution
- ✅ Memory persistence across sessions (CosmosDB + AI Search)
- ✅ Multi-agent orchestration with semantic reasoning

### Quality Requirements
- ✅ API latency P95 < 500ms for tool calls
- ✅ Agent evaluation scores > 0.85 for task adherence
- ✅ Zero secrets in code or logs
- ✅ 100% authentication for all endpoints

### Operational Requirements
- ✅ Infrastructure provisioned via azd in < 30 minutes
- ✅ Zero-downtime deployments via AKS rolling updates
- ✅ Observability dashboards available within 5 minutes of deployment
- ✅ Reinforcement-learning loop executable end-to-end

## Constraints & Assumptions

### Technical Constraints
- Python 3.10+ for agent implementations
- Azure Kubernetes Service as container orchestrator
- Azure API Management for gateway (Developer or Standard tier)
- Azure AI Foundry for model hosting and evaluation
- CosmosDB for stateful storage with vector search

### Security Constraints
- All traffic encrypted in transit (TLS 1.2+)
- Secrets stored only in Azure Key Vault
- Network isolation via VNet and private endpoints
- Audit logging for all authentication events

### Compliance Assumptions
- Agents operate within Microsoft cloud boundary
- Data residency controls enforced per regulatory requirements
- Human oversight available for high-risk decisions
- Retention policies aligned with business requirements

## Governance

### Change Management
- Specification changes require explicit documentation
- Breaking changes trigger version bumps
- All new agents require corresponding tests and evaluation baselines

### Review Process
- Specification reviewed before implementation
- Code reviews for all agent logic changes
- Evaluation results reviewed before production deployment
- Security review for identity and access changes

### Lifecycle Management
- Agents versioned with semantic versioning
- Deprecated agents have 90-day sunset period
- Policy versions tracked with lineage across learning runs
- Evaluation baselines maintained for regression detection

## Solution Architecture Summary

### Eight Pillars of Enterprise Agent Governance

| # | Pillar | Azure Service | Purpose |
|---|--------|---------------|---------|
| 1 | API Governance | APIM + MCP | Centralized policies, rate limits, routing |
| 2 | Secure Runtime | AKS + VNet | Private networking, workload identity |
| 3 | Enterprise Memory | CosmosDB + AI Search + Fabric IQ | Short/long-term memory, ontology facts |
| 4 | Agent Orchestration | Azure AI Foundry | Multi-agent coordination, tool catalog |
| 5 | Identity & Access | Microsoft Entra ID | Agent identities, RBAC, conditional access |
| 6 | Observability | Azure Monitor + App Insights | Telemetry, tracing, compliance proof |
| 7 | Secrets & Artifacts | Key Vault + Storage | Keyless auth, artifact governance |
| 8 | Human Oversight | Agent 365 | Human-in-the-loop, approval workflows |

## Lab Integration

This constitution supports the Azure Agents Control Plane Lab with the following exercises:

1. **Lab Intro.** (30 min): Environment validation and architecture walkthrough
2. **Build Agents** (1 hr): Specification-driven agent development with Copilot
3. **End-to-End Review** (30 min): Governance verification across all pillars
4. **Learning** (1 hr): Azure Agents Learning SDK workflow for behavior optimization
5. **Evaluations** (1 hr): Evaluation framework for task adherence and quality

**Success Criteria**: "Copilot helped me move faster, but Microsoft Azure, Foundry and Fabric made this enterprise ready."

## References

- [Azure AI Foundry](https://learn.microsoft.com/azure/ai-foundry/what-is-foundry)
- [Azure AI Foundry Agent Service](https://learn.microsoft.com/azure/ai-foundry/agents/overview)
- [Model Context Protocol for Azure](https://learn.microsoft.com/azure/developer/azure-mcp-server/tools/azure-foundry)
- [Microsoft Entra Agent Identity](https://learn.microsoft.com/entra/agent-id/identity-professional/microsoft-entra-agent-identities-for-ai-agents)
- [AKS Static Egress Gateway](https://learn.microsoft.com/azure/aks/configure-static-egress-gateway)
- [Azure Monitor & App Insights](https://learn.microsoft.com/azure/azure-monitor/overview)
