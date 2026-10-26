# Fabric Data Agents Setup Guide

The Python MCP runtime provides four specialists, an orchestrator, and OneLake
file operations. The TypeScript runtime does not implement these additions.

**Fabric stays disabled in the default deployment.** Neither `azd up` nor the
post-provision hooks automatically create a workspace or enable these tools.
Setting azd environment values alone does not override the disabled Kubernetes
template. Provision resources and opt into runtime access separately.

## Capabilities and boundaries

| Component | Behavior |
|-----------|----------|
| Lakehouse specialist | Execute Spark SQL, discover resources, and explore table schemas |
| Warehouse specialist | Execute T-SQL and explore `INFORMATION_SCHEMA` |
| Pipeline specialist | Trigger, monitor, wait for completion, and retry bounded runs |
| Semantic Model specialist | Delegate DAX/MDX requests to the existing Fabric tools |
| Orchestrator | Route by explicit agent type, resource ID, then keywords; aggregate cross-domain responses |
| OneLake client | List, read, write, delete, and inspect files through the DFS endpoint |

Routing and intent detection are heuristics, not an LLM query generator. Supply
executable SQL/DAX/MDX for data queries. Cross-domain calls delegate sequentially
and aggregate responses; they do not generate queries, join results, or plan
dependencies between operations. Default row limits are best-effort query
conveniences, not authorization or resource-quota enforcement.

The specialists reuse [fabric_tools.py](../src/fabric_tools.py). Mocked tests
verify local behavior, not the availability of its query endpoints in a tenant.
Confirm supported APIs, query engines, permissions, and query languages for your
Fabric environment before production use.

## Prerequisites

- Python 3.11+ and dependencies from [requirements.txt](../src/requirements.txt).
- PowerShell 7, Azure CLI, and Azure Developer CLI for the optional provisioner.
- An active Fabric capacity that supports the intended items, and permission to
  create workspaces/items and assign the capacity.
- Fabric tenant settings permitting the runtime's service principal, appropriate
  workspace/item access for that identity, and the required network connectivity.
- For AKS, the existing approved workload-identity, private networking, build,
  approval, and registry configuration. Do not substitute resource keys or
  bypass the deployment gates.

Azure capacity roles alone do not grant Fabric workspace/item access. Grant
only the workspace, item, and semantic-model permissions the requested operations
need. The Fabric and OneLake clients use `DefaultAzureCredential`; AKS normally
uses the existing UAMI through workload identity. Agent 365 registration does not
transfer those permissions or change these clients to an Entra Agent ID.

Capacity provisioning is separate from workspace creation. See the
[capacity template](../infra/core/fabric/fabric-capacity.bicep) and
[Fabric capacity documentation](https://learn.microsoft.com/fabric/enterprise/buy-subscription).
Check current regional pricing rather than relying on an embedded cost estimate.

## Explicit workspace provisioning

Authenticate to the intended tenant/subscription and select an azd environment:

```powershell
az login --tenant "<TENANT_ID>"
az account set --subscription "<SUBSCRIPTION_ID>"
azd env select "<ENVIRONMENT>"

.\scripts\deploy-fabric-workspace.ps1 `
    -Environment "<ENVIRONMENT>" `
    -WorkspaceName "<WORKSPACE_NAME>" `
    -CapacityId "<FABRIC_CAPACITY_GUID>"
```

Run this only after approving the resource creation and associated costs.
`-CapacityId` is the Fabric API capacity GUID, not an Azure resource ID. If it is
omitted, the script resolves `FABRIC_CAPACITY_NAME` from the selected environment,
or reuses an existing workspace's capacity. A new workspace requires a capacity.

The script creates or reuses:

- The named workspace.
- A Lakehouse named `AgentsLakehouse`.
- A Warehouse named `AgentsWarehouse`.

It handles paginated resource discovery and polls HTTP 202 long-running
operations before using resource IDs. Capacity-assignment, API, missing-ID, and
azd-save failures stop the script; a failed operation is not reported as complete.
It saves `FABRIC_WORKSPACE_ID`, `FABRIC_LAKEHOUSE_ID`, `FABRIC_WAREHOUSE_ID`, and
`FABRIC_LAKEHOUSE_NAME` to the selected azd environment. It does **not** enable the
runtime, grant workspace permissions, upload ontologies, create a pipeline or
semantic model, build an image, or deploy to AKS.

Create pipelines and semantic models separately. Uploading a CSV to `Files`
does not create a queryable Lakehouse table; load/register it as a table in Fabric
before querying it.

## Runtime configuration

Set configuration before the Python process imports the Fabric modules. The MCP
server loads its local `.env` before those imports; restart after changing values.
See [.env.example](../src/.env.example) for disabled-by-default settings.

| Variable | Default | Meaning |
|----------|---------|---------|
| `FABRIC_ENABLED` | `false` | Existing Fabric integration flag |
| `FABRIC_DATA_AGENTS_ENABLED` | `false` | Enable the orchestrator and OneLake tool client |
| `FABRIC_WORKSPACE_ID` | empty | Workspace GUID |
| `FABRIC_API_ENDPOINT` | `https://api.fabric.microsoft.com/v1` | Existing Fabric REST client endpoint |
| `FABRIC_ONELAKE_DFS_ENDPOINT` | `https://onelake.dfs.fabric.microsoft.com` | OneLake DFS endpoint |
| `FABRIC_AGENT_MAX_RETRIES` | `3` | Maximum attempts for an explicit pipeline retry |
| `FABRIC_PIPELINE_POLL_INTERVAL` | `30` | Seconds between status polls |
| `FABRIC_PIPELINE_TIMEOUT` | `3600` | Pipeline polling deadline in seconds |
| `ONELAKE_MAX_DOWNLOAD_BYTES` | `52428800` | Maximum downloaded file bytes (50 MiB) |

Numeric settings must be positive integers. OneLake checks both the declared
file size and the actual streamed byte count. Binary reads return base64 with
`isText=false`; writes accept UTF-8 text. `overwrite=false` uses a conditional
create and stops if the file already exists. File operations require a non-empty
path inside `Files` or `Tables`; listing may use an empty path for that section's
root. Create parent directories beforehand.

### Local process opt-in

From the repository root, after configuring the existing MCP dependencies:

```powershell
$env:FABRIC_ENABLED = "true"
$env:FABRIC_DATA_AGENTS_ENABLED = "true"
$env:FABRIC_WORKSPACE_ID = "<WORKSPACE_GUID>"
$env:FABRIC_API_ENDPOINT = "https://api.fabric.microsoft.com/v1"
$env:FABRIC_ONELAKE_DFS_ENDPOINT = "https://onelake.dfs.fabric.microsoft.com"

Push-Location .\src
try {
    ..\.venv\Scripts\python.exe -m uvicorn next_best_action_agent:app --host 127.0.0.1 --port 8000
} finally {
    Pop-Location
}
```

The clients acquire tokens when operations need them. Do not expose the local
server publicly or put tokens, signed URLs, or resource credentials into `.env`.

### AKS opt-in for an approved Python deployment

Build and deploy through the existing gated workflow described in the
[README](../README.md#deployment). The Python Dockerfile already copies all
application modules; no separate image or additional dependency is needed.

After the existing identity/approval preflight succeeds and an operator approves
Fabric access for that deployment, apply a namespace-scoped environment override:

```powershell
kubectl set env deployment/mcp-agents -n mcp-agents `
    FABRIC_ENABLED=true `
    FABRIC_DATA_AGENTS_ENABLED=true `
    FABRIC_WORKSPACE_ID="<WORKSPACE_GUID>" `
    FABRIC_API_ENDPOINT=https://api.fabric.microsoft.com/v1 `
    FABRIC_ONELAKE_DFS_ENDPOINT=https://onelake.dfs.fabric.microsoft.com
kubectl rollout status deployment/mcp-agents -n mcp-agents --timeout=300s
```

This does not grant Fabric permissions or bypass the existing deployment gates.
The standard post-provision hooks keep Fabric disabled and will reset this
override on a later deployment. Manage intentional overrides through your
approved deployment process. To disable these tools, set
`FABRIC_DATA_AGENTS_ENABLED=false` and wait for the rollout.

## MCP tool reference

These eight tools supplement the existing six low-level `fabric_*` tools.
Failures are returned as MCP tool errors as well as unsuccessful JSON results.

| Tool | Required arguments | Optional arguments |
|------|--------------------|--------------------|
| `fabric_agent_query` | `query` | `agent_type`, resource IDs, `sql_query`, `run_id`, `pipeline_parameters`, `wait_for_completion`, `query_language` |
| `fabric_agent_cross_domain` | `query` | Resource IDs, per-specialist `queries`, pipeline parameters/status options, `query_language` |
| `fabric_agent_list_all` | none | none |
| `onelake_list_files` | `lakehouse_id` | `path`, `section`, `recursive` |
| `onelake_read_file` | `lakehouse_id`, `path` | `section` |
| `onelake_write_file` | `lakehouse_id`, `path`, `content` | `section`, `overwrite` (default `true`) |
| `onelake_delete_file` | `lakehouse_id`, `path` | `section`, `recursive` (default `false`) |
| `onelake_get_file_properties` | `lakehouse_id`, `path` | `section` |

Resource IDs are `lakehouse_id`, `warehouse_id`, `pipeline_id`, and `dataset_id`.
Agent types are `lakehouse`, `warehouse`, `pipeline`, and `semantic_model`.
`section` is `Files` (default) or `Tables`. Use `query`, not `user_query`, in
MCP arguments; use `lakehouse_id`, not `item_id`, for OneLake tools.

### Lakehouse query

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "fabric_agent_query",
    "arguments": {
      "query": "query the lakehouse for customers",
      "agent_type": "lakehouse",
      "lakehouse_id": "<LAKEHOUSE_GUID>",
      "sql_query": "SELECT customer_id, name FROM customers LIMIT 10"
    }
  }
}
```

### Monitor an existing pipeline run

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "fabric_agent_query",
    "arguments": {
      "query": "check pipeline run status",
      "agent_type": "pipeline",
      "pipeline_id": "<PIPELINE_GUID>",
      "run_id": "<RUN_GUID>"
    }
  }
}
```

To trigger a run, use `query: "trigger pipeline"` and `pipeline_parameters`.
With `wait_for_completion=true`, failed, cancelled, timed-out, or unreadable
terminal status is an error rather than a successful trigger response.

### Upload UTF-8 text without overwriting

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "onelake_write_file",
    "arguments": {
      "lakehouse_id": "<LAKEHOUSE_GUID>",
      "path": "report.csv",
      "content": "id,value\n1,100\n",
      "overwrite": false
    }
  }
}
```

For Python callers, add `src` to `PYTHONPATH` and use
`get_onelake_client().write_file(lakehouse_id="...", path="report.csv", content="...")`
from [fabric_onelake.py](../src/fabric_onelake.py).

For cross-domain data calls, `queries` maps `lakehouse`, `warehouse`, and/or
`semantic_model` to separate executable queries in each specialist's language.

## Offline validation

Run from the repository root with the development dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pytest .\tests\test_fabric_data_agents.py -q
.\.venv\Scripts\python.exe -m pytest .\tests\test_next_best_action_unit.py::TestFabricMcpIntegration -q
```

The first suite mocks HTTP and credentials. Where PowerShell 7 is installed, it
also executes the real provisioner with in-memory Azure CLI, azd, and REST
functions; it never provisions resources. Coverage includes intent precedence,
query forwarding, schema failures, bounded pipeline retries/polling, feature
disable paths, OneLake pagination/paths, exact download limits, conditional
writes, provisioning failures, long-running operations, and deployment defaults.
The second suite checks registration, MCP parameter forwarding, and error flags.

These checks do **not** validate live Fabric/Power BI query API compatibility,
tenant licensing/consent, workload-identity permissions, container execution,
private endpoint reachability, or a deployed AKS rollout. Validate those in an
approved test environment separately; a mocked success is not a deployment result.

## Troubleshooting

- **Tools disabled:** check the running pod/process configuration, not just azd
  values. The default template deliberately keeps Fabric off.
- **401/403:** verify tenant settings, the credential actually selected by
  `DefaultAzureCredential`, and Fabric workspace/item permissions. Registering an
  Agent ID does not grant UAMI access.
- **Unsupported query API/language:** confirm the existing
  [Fabric client](../src/fabric_tools.py) endpoints against supported APIs for
  your workload. Do not treat mocked responses as service validation.
- **No queryable table:** a file in OneLake `Files` is not automatically a table.
- **Capacity/item creation fails:** confirm capacity availability and assignment;
  rerun only after resolving the reported error.
- **File already exists:** choose a new path or explicitly allow overwriting.
- **File too large:** use a smaller file or deliberately raise the byte limit
  within the pod's memory budget; base64 and JSON encoding also consume memory.

See the [OneLake API guidance](https://learn.microsoft.com/fabric/onelake/onelake-access-api)
and [Fabric long-running operation contract](https://learn.microsoft.com/rest/api/fabric/articles/long-running-operation).
