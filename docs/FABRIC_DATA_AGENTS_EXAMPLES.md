# Fabric Data Agents - Usage Examples

This document provides practical examples of using Fabric Data Agents in the Azure Agents Control Plane.

For current provisioning, explicit runtime opt-in, and the eight additional
Python orchestrator/OneLake tools, start with the
[setup guide](FABRIC_DATA_AGENTS_SETUP_GUIDE.md). The examples below describe the
existing low-level tools. Default deployments keep Fabric disabled; mock
responses here do not establish live API compatibility.

## Overview

Fabric Data Agents enable enterprise AI agents to interact with Microsoft Fabric's data platform capabilities:

- **Lakehouse Agent**: Query/write Spark SQL against Fabric Lakehouses
- **Warehouse Agent**: Execute T-SQL queries against Fabric Data Warehouses
- **Pipeline Agent**: Trigger, monitor, and manage Fabric Data Pipelines
- **Semantic Model Agent**: Query Power BI semantic models via DAX/MDX

## Prerequisites

1. **Fabric Capacity**: Microsoft Fabric capacity deployed (F2 or higher)
2. **Fabric Workspace**: Workspace created with appropriate resources
3. **RBAC**: Agent identity assigned Reader and Contributor roles
4. **Configuration**: Environment variables set in K8s deployment

## Configuration

### Infrastructure Deployment

Fabric capacity provisioning and runtime enablement are separate, opt-in
operations. Changing Bicep parameters or azd environment values alone does not
enable the disabled Kubernetes template. Follow the
[setup guide](FABRIC_DATA_AGENTS_SETUP_GUIDE.md#explicit-workspace-provisioning)
for capacity/workspace prerequisites and the approved runtime override; keep the
existing identity, approval, and private-registry deployment gates in place.

### Environment Variables

Verify in K8s deployment (`k8s/mcp-agents-deployment.yaml`):

```yaml
- name: FABRIC_ENABLED
  value: "true"
- name: FABRIC_DATA_AGENTS_ENABLED
  value: "true"
- name: FABRIC_API_ENDPOINT
  value: "https://api.fabric.microsoft.com/v1"
- name: FABRIC_WORKSPACE_ID
  value: "abcd1234-5678-90ab-cdef-ghijklmnopqr"
```

## Example 1: Customer Churn Analysis (Lakehouse)

Query a Fabric Lakehouse to analyze customer churn risk.

**Scenario**: AI agent needs to identify high-risk customers for retention campaigns.

### MCP Tool Call

```json
{
  "tool": "fabric_query_lakehouse",
  "arguments": {
    "lakehouse_id": "lh-customer-analytics-001",
    "lakehouse_name": "CustomerAnalytics",
    "query": "SELECT customer_id, name, email, churn_risk, segment, monthly_spend FROM customers WHERE churn_risk > 0.7 ORDER BY churn_risk DESC LIMIT 20"
  }
}
```

### Response

```json
{
  "success": true,
  "lakehouse_id": "lh-customer-analytics-001",
  "lakehouse_name": "CustomerAnalytics",
  "results": {
    "schema": [
      {"name": "customer_id", "type": "string"},
      {"name": "name", "type": "string"},
      {"name": "email", "type": "string"},
      {"name": "churn_risk", "type": "float"},
      {"name": "segment", "type": "string"},
      {"name": "monthly_spend", "type": "float"}
    ],
    "rows": [
      {
        "customer_id": "CUST001",
        "name": "Acme Corp",
        "email": "contact@acme.com",
        "churn_risk": 0.92,
        "segment": "enterprise",
        "monthly_spend": 15000.00
      },
      ...
    ]
  },
  "timestamp": "2026-02-07T19:50:00Z"
}
```

### Agent Action

The agent can now:
1. Generate retention recommendations for each high-risk customer
2. Trigger automated retention workflows
3. Alert account managers

## Example 2: Sales Performance Analysis (Warehouse)

Query a Fabric Data Warehouse for sales analytics.

**Scenario**: AI agent needs to analyze top-performing sales regions for executive dashboard.

### MCP Tool Call

```json
{
  "tool": "fabric_query_warehouse",
  "arguments": {
    "warehouse_id": "wh-sales-dwh-prod",
    "warehouse_name": "SalesDataWarehouse",
    "query": "SELECT TOP 10 region, SUM(revenue) as total_revenue, COUNT(DISTINCT customer_id) as customer_count, AVG(deal_size) as avg_deal_size FROM sales_summary WHERE sale_date >= DATEADD(month, -3, GETDATE()) GROUP BY region ORDER BY total_revenue DESC"
  }
}
```

### Response

```json
{
  "success": true,
  "warehouse_id": "wh-sales-dwh-prod",
  "warehouse_name": "SalesDataWarehouse",
  "results": {
    "schema": [
      {"name": "region", "type": "varchar"},
      {"name": "total_revenue", "type": "decimal"},
      {"name": "customer_count", "type": "int"},
      {"name": "avg_deal_size", "type": "decimal"}
    ],
    "rows": [
      {
        "region": "North America",
        "total_revenue": 5250000.00,
        "customer_count": 342,
        "avg_deal_size": 15350.58
      },
      {
        "region": "EMEA",
        "total_revenue": 3890000.00,
        "customer_count": 267,
        "avg_deal_size": 14570.41
      },
      ...
    ]
  },
  "timestamp": "2026-02-07T19:50:00Z"
}
```

## Example 3: Data Pipeline Orchestration (Pipeline)

Trigger and monitor a Fabric Data Pipeline for ETL operations.

**Scenario**: AI agent needs to refresh customer churn predictions on demand.

### Step 1: Trigger Pipeline

```json
{
  "tool": "fabric_trigger_pipeline",
  "arguments": {
    "pipeline_id": "pl-churn-prediction-etl",
    "pipeline_name": "ChurnPredictionETL",
    "parameters": "{\"run_date\": \"2026-02-07\", \"full_refresh\": false, \"model_version\": \"v2.3\"}"
  }
}
```

### Response

```json
{
  "success": true,
  "pipeline_id": "pl-churn-prediction-etl",
  "pipeline_name": "ChurnPredictionETL",
  "run_id": "run-abc123-def456-789",
  "status": "InProgress",
  "parameters": {
    "run_date": "2026-02-07",
    "full_refresh": false,
    "model_version": "v2.3"
  },
  "timestamp": "2026-02-07T19:50:00Z"
}
```

### Step 2: Monitor Pipeline Status

Wait for pipeline completion (poll every 30 seconds):

```json
{
  "tool": "fabric_get_pipeline_status",
  "arguments": {
    "pipeline_id": "pl-churn-prediction-etl",
    "run_id": "run-abc123-def456-789",
    "pipeline_name": "ChurnPredictionETL"
  }
}
```

### Response

```json
{
  "success": true,
  "pipeline_id": "pl-churn-prediction-etl",
  "run_id": "run-abc123-def456-789",
  "status": "Succeeded",
  "start_time": "2026-02-07T19:50:00Z",
  "end_time": "2026-02-07T19:53:45Z",
  "duration": "00:03:45",
  "details": {
    "activities_completed": 12,
    "rows_processed": 150000
  },
  "timestamp": "2026-02-07T19:54:00Z"
}
```

## Example 4: Power BI Analytics (Semantic Model)

Query a Power BI semantic model using DAX.

**Scenario**: AI agent needs to retrieve KPIs from a pre-built Power BI dashboard.

### MCP Tool Call

```json
{
  "tool": "fabric_query_semantic_model",
  "arguments": {
    "dataset_id": "ds-customer-360-kpis",
    "dataset_name": "Customer360KPIs",
    "query": "EVALUATE TOPN(10, ADDCOLUMNS(Customer, \"ChurnScore\", [Measures].[ChurnRisk], \"LTV\", [Measures].[LifetimeValue]), [ChurnScore], DESC)",
    "query_language": "DAX"
  }
}
```

### Response

```json
{
  "success": true,
  "dataset_id": "ds-customer-360-kpis",
  "dataset_name": "Customer360KPIs",
  "query_language": "DAX",
  "results": {
    "schema": [
      {"name": "CustomerID", "type": "string"},
      {"name": "CustomerName", "type": "string"},
      {"name": "ChurnScore", "type": "number"},
      {"name": "LTV", "type": "currency"}
    ],
    "rows": [
      {
        "CustomerID": "CUST001",
        "CustomerName": "Acme Corp",
        "ChurnScore": 0.92,
        "LTV": 450000.00
      },
      ...
    ]
  },
  "timestamp": "2026-02-07T19:50:00Z"
}
```

## Example 5: Resource Discovery

Discover available Fabric resources in the workspace.

### MCP Tool Call

```json
{
  "tool": "fabric_list_resources",
  "arguments": {
    "resource_type": "all"
  }
}
```

### Response

```json
{
  "success": true,
  "workspace_id": "abcd1234-5678-90ab-cdef-ghijklmnopqr",
  "resource_type": "all",
  "resources": {
    "lakehouses": [
      {
        "id": "lh-customer-analytics-001",
        "name": "CustomerAnalytics",
        "description": "Customer behavior and churn analytics"
      },
      {
        "id": "lh-devops-telemetry-001",
        "name": "DevOpsTelemetry",
        "description": "CI/CD pipeline metrics and logs"
      }
    ],
    "warehouses": [
      {
        "id": "wh-sales-dwh-prod",
        "name": "SalesDataWarehouse",
        "description": "Enterprise sales data warehouse"
      }
    ],
    "pipelines": [
      {
        "id": "pl-churn-prediction-etl",
        "name": "ChurnPredictionETL",
        "description": "Daily customer churn prediction pipeline"
      },
      {
        "id": "pl-devops-aggregation",
        "name": "DevOpsMetricsAggregation",
        "description": "Aggregate pipeline success rates"
      }
    ],
    "semantic_models": [
      {
        "id": "ds-customer-360-kpis",
        "name": "Customer360KPIs",
        "description": "Customer 360 KPIs and metrics"
      }
    ]
  },
  "timestamp": "2026-02-07T19:50:00Z"
}
```

## Example 6: Memory Integration - Load Entities from Lakehouse

Synchronize customer entities from Fabric Lakehouse into Facts Memory.

### Python Code (in Agent)

```python
from memory import FactsMemory, EntityType

# Initialize facts memory
facts_memory = FactsMemory(
    fabric_enabled=True,
    fabric_endpoint="https://api.fabric.microsoft.com/v1",
    workspace_id="abcd1234-5678-90ab-cdef-ghijklmnopqr"
)

# Load customer entities from lakehouse
count = await facts_memory.load_entities_from_lakehouse(
    lakehouse_id="lh-customer-analytics-001",
    table_name="customers",
    entity_type=EntityType.CUSTOMER,
    id_column="customer_id",
    property_columns=["name", "email", "segment", "churn_risk", "monthly_spend"]
)

print(f"Loaded {count} customer entities from Fabric Lakehouse")

# Query entities with high churn risk
high_risk_customers = await facts_memory.query_entities(
    entity_type=EntityType.CUSTOMER,
    filters={"churn_risk": lambda x: x > 0.7},
    limit=20
)

# Generate recommendations
for customer in high_risk_customers:
    print(f"High risk: {customer.properties['name']} (risk: {customer.properties['churn_risk']:.0%})")
```

## Example 7: Memory Integration - Sync Facts from Warehouse

Synchronize derived facts from Fabric Warehouse into Facts Memory.

### Python Code (in Agent)

```python
# Sync customer insights facts from warehouse
count = await facts_memory.sync_facts_from_warehouse(
    warehouse_id="wh-sales-dwh-prod",
    fact_table="customer_insights",
    domain="customer",
    statement_column="insight",
    confidence_column="confidence_score"
)

print(f"Synced {count} facts from Fabric Warehouse")

# Search for churn-related facts
churn_facts = await facts_memory.search_facts(
    query="customer churn retention",
    domain="customer",
    limit=10
)

for result in churn_facts:
    fact = result.fact
    print(f"[{fact.confidence:.0%}] {fact.statement}")
```

## Agent Conversation Examples

### Example: Customer Retention Agent

**User**: "Which customers are at high risk of churning and what should we do about them?"

**Agent Actions**:
1. Call `fabric_query_lakehouse` to find high-risk customers
2. Call `fabric_query_semantic_model` to get customer LTV and engagement metrics
3. Load entities into Facts Memory
4. Generate retention recommendations
5. Call `fabric_trigger_pipeline` to trigger retention campaign

**Agent Response**: 
> "I've identified 15 high-risk customers (churn probability > 70%) with a combined LTV of $2.1M. The top 3 are:
> 
> 1. **Acme Corp** (92% churn risk, $450K LTV)
>    - Recommendation: Immediate account manager outreach + 20% discount offer
> 
> 2. **TechCo Inc** (88% churn risk, $380K LTV)
>    - Recommendation: Technical review + executive engagement
> 
> 3. **Global Services** (85% churn risk, $350K LTV)
>    - Recommendation: Contract renewal incentive + premium support
> 
> I've triggered the retention campaign pipeline and it will complete in ~5 minutes."

### Example: DevOps Analytics Agent

**User**: "Show me the success rate of our CI/CD pipelines over the past week"

**Agent Actions**:
1. Call `fabric_list_resources` to discover available pipelines
2. Call `fabric_query_warehouse` to get pipeline success metrics
3. Generate trend analysis

**Agent Response**:
> "Here's the CI/CD pipeline analysis for the past week:
> 
> **Overall Success Rate**: 87.3% (down 3.2% from last week)
> 
> **Top Performers**:
> - Frontend Pipeline: 95.1% success
> - API Gateway Pipeline: 93.8% success
> 
> **Needs Attention**:
> - ML Model Pipeline: 72.4% success (frequent timeout issues)
> - Database Migration Pipeline: 68.9% success (schema conflicts)
> 
> Would you like me to trigger a diagnostic pipeline run for the problematic pipelines?"

## Best Practices

### 1. Error Handling

Always check for errors in tool responses:

```python
result = fabric_query_lakehouse_tool(lakehouse_id, query)
data = json.loads(result)

if not data.get('success'):
    error = data.get('error', 'Unknown error')
    logger.error(f"Lakehouse query failed: {error}")
    # Handle error appropriately
```

### 2. Query Optimization

- Use `LIMIT` in Spark SQL queries to avoid large result sets
- Use `TOP` in T-SQL queries for similar reasons
- Index frequently queried columns in your lakehouses/warehouses

### 3. Pipeline Monitoring

- Always capture `run_id` from pipeline trigger responses
- Poll pipeline status at reasonable intervals (30-60 seconds)
- Set timeout thresholds for long-running pipelines

### 4. Security

- Never expose workspace IDs or resource IDs in user-facing messages
- All queries are executed with agent identity RBAC permissions
- Fabric automatically enforces row-level security (RLS) if configured

### 5. Performance

- Cache frequently accessed data in Facts Memory
- Use warehouse aggregation tables instead of querying raw lakehouse data
- Leverage semantic models for pre-computed KPIs

## Testing

Run the integration test suite:

```bash
cd tests
python test_fabric_agents.py
```

See `tests/test_fabric_agents.py` for comprehensive test cases.

## Troubleshooting

### Issue: "Fabric Data Agents not enabled"

**Solution**: Verify environment variables:
```bash
kubectl get deployment mcp-agents -n mcp-agents -o yaml | grep FABRIC
```

Expected output:
```
- name: FABRIC_ENABLED
  value: "true"
- name: FABRIC_DATA_AGENTS_ENABLED
  value: "true"
```

### Issue: "Authentication failed"

**Solution**: Verify workload identity configuration:
```bash
# Check service account annotations
kubectl get sa mcp-agents-sa -n mcp-agents -o yaml

# Check federated credentials
az identity federated-credential list \
  --identity-name id-mcp-${AZURE_ENV_NAME} \
  --resource-group rg-${AZURE_ENV_NAME}
```

### Issue: "Workspace not found"

**Solution**: Verify workspace ID in configuration:
```bash
# Check deployment env vars
kubectl get deployment mcp-agents -n mcp-agents -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FABRIC_WORKSPACE_ID")].value}'
```

## Additional Resources

- [Microsoft Fabric REST API Documentation](https://learn.microsoft.com/en-us/rest/api/fabric/articles/)
- [Fabric Python SDK](https://learn.microsoft.com/en-us/python/api/overview/azure/synapse-artifacts)
- [Fabric Workload Identity](https://learn.microsoft.com/en-us/fabric/security/security-overview)
- [AGENTS_ARCHITECTURE.md](../docs/AGENTS_ARCHITECTURE.md) - Full architecture documentation
