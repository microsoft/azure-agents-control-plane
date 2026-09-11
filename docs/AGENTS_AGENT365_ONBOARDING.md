# Agent 365: register once, assign accountability, add telemetry

For this existing AKS service, use **standard Agent 365 onboarding**, not an
AI teammate, a new Teams bot, or our custom directory-provisioning automation.
Keep AKS, private ACR, Cosmos, Storage, and the working Logic App/Teams approval
flow. No mailbox, Work IQ permissions, or new messaging endpoint is needed.

## What is required (and what is not)

The seven `AgentIdentityBlueprint.*` / `AgentIdentityBlueprintPrincipal.*`
application permissions previously requested for the configuration UAMI are
**only for this repository's custom provisioning scripts**. They are **not**
required on that UAMI to use an already-created Agent ID or emit telemetry.

Microsoft's [existing-agent quickstart](https://learn.microsoft.com/microsoft-agent-365/developer/get-started)
uses the Agent 365 CLI/skills to create a blueprint, a real Entra Agent ID, and
a catalog registration. The [CLI reference](https://learn.microsoft.com/microsoft-agent-365/developer/reference/cli/setup)
supports externally hosted agents and the Microsoft-managed CLI enterprise app
when it is available in the tenant. No custom management app on AKS is needed.

This is simpler, **not permissionless**:

- An Agent 365-enabled tenant and supported **Agent ID Developer** access are
  prerequisites for creating a new blueprint. A preapproved/platform-provided
  blueprint/identity can instead be reused.
- Existing client consent and tenant policy still apply. The CLI hands off
  missing OAuth admin consent; S2S application grants also require an authorized
  tenant administrator. The default OBO grant step's "no admin role" wording
  does **not** remove these prerequisites or give this autonomous service a user.
- Telemetry requires `Agent365.Observability.OtelWrite` **application** permission
  for S2S, plus at least one user in the tenant with an assigned Microsoft 365 E7
  or Microsoft Agent 365 license. A SKU merely present in the tenant is not enough.
- Azure subscription Owner is not an Entra directory role. Changing CLI tools,
  device-code flows, or registering ordinary metadata cannot bypass tenant policy.

If developer access/consent cannot be obtained in the Non-Production tenant,
use an identity provisioned by an approved platform team or an approved development
tenant. Do not retry permission writes from the blocked device. Agent identities
are tenant-local: our hosting and Teams-approver tenants are different. This
secretless integration requires the Agent ID and bootstrap UAMI in the **hosting
tenant**; moving registration to the Teams tenant is not a drop-in workaround.

## 1. Register and verify the owner

On a permitted device/tenant, follow the official quickstart's **standard agent**
path (registration only, externally hosted). Review a dry-run before applying.
The corresponding current CLI preview is:

```powershell
a365 setup all --agent-name next-best-action --tenant-id <hosting-tenant-id> --authmode s2s --dry-run
```

After review and prerequisite approval, the same command without `--dry-run`
performs onboarding. **It is a directory/registration write**, not a local check.
Do not use `--m365`, `--aiteammate`, or supply the MCP URL as a Bot Framework
messaging endpoint. Do not run `setup requirements` as a read-only check: it can
repair prerequisites. Stop on missing permissions or uncertain creation; preserve
the CLI's state instead of creating another registration.

The CLI attempts to set the signed-in developer as blueprint owner and sponsor.
Verify the resulting **agent identity sponsor, blueprint owner, and catalog
owner** rather than assuming all three are identical. Use the intended human's
object ID in that tenant; a sponsor does not need to be an administrator. The
[lifecycle actions page](https://learn.microsoft.com/microsoft-365/admin/manage/agent-actions)
restricts its "Assign new owner" action to shared Agent Builder/Copilot Studio
agents; it is not a universal owner-assignment API for arbitrary Python agents.

Retain the real Agent ID, blueprint client ID, blueprint application object ID,
and registration ID from the completed onboarding. Check Entra **Agents → Agent
identities / Agent blueprints** and Microsoft 365 **Agents → All agents → Registry**.
Registration is not proved by a locally generated GUID or an inventory package ID.

## 2. Adopt the IDs; leave Azure access alone

The default `AGENT_IDENTITY_PROVISIONING_MODE=adopt` does not run Bicep's directory
creation/reconciliation modules. The [deployment check](../scripts/deployment_gate.py)
reads the real blueprint/child relationship and existing federation; it does not
read management-role assignments or grant anything in this mode.

Use the following settings in the **named azd environment**:

| Setting | Minimal integration |
| --- | --- |
| `AGENT_IDENTITY_PROVISIONING_MODE` | `adopt` (default) |
| `AGENT_IDENTITY_ENABLED` | `false`: Azure data clients/approvals keep their existing UAMI; this does **not** disable the separately registered Entra identity |
| `AGENT_REGISTRY_ENABLED` | `false`: do not run a second, repository-owned Graph publisher after the CLI has registered the agent |
| `AGENT_OBSERVABILITY_MODE` | `console` for local validation, then `agent365` after authorized onboarding |
| `AGENT_IDENTITY_APP_ID` / `AGENT_IDENTITY_PRINCIPAL_ID` | The actual child identity (CLI generated `agenticAppId`) |
| `AGENT_IDENTITY_BLUEPRINT_APP_ID` | Blueprint client ID (CLI `agentBlueprintId`) |
| `AGENT_IDENTITY_BLUEPRINT_OBJECT_ID` | Blueprint **application** object ID (CLI `agentBlueprintObjectId`) |
| `AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID` | Optional blueprint service-principal ID (CLI `agentBlueprintServicePrincipalObjectId`) |
| `EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID` / `EXISTING_AGENT_IDENTITY_ID` | Same blueprint client ID / child ID, for future Bicep adoption |
| `AGENT_IDENTITY_DISPLAY_NAME` | Registered agent name |

CLI `agentRegistrationId` is **not** the child ID and is not necessarily compatible
with this repository's `AGENT_REGISTRY_ID` API/journal. Keep the CLI-managed
registration in its own state; never send it to the alternate publisher blindly.
Generated onboarding files can contain protected secrets: they are ignored by
Git and must not be printed, committed, or copied into a container image. Only
the nonsecret identity fields above are consumed here; the app does not import
the CLI's generated configuration wholesale.

For this repository's **secretless** S2S credential, the blueprint owner must
configure an existing federated credential trusting the hosting UAMI:

- issuer: `https://login.microsoftonline.com/<hosting-tenant-id>/v2.0`
- subject: bootstrap UAMI **principal/object ID**, not client ID
- audience: `api://AzureADTokenExchange`

Any FIC name is accepted in adopt mode if the trust matches. The UAMI itself
needs **no** blueprint-management permissions. A CLI-created client secret alone
does not configure this trust; this runtime deliberately does not import it.
The build reader still needs permission to **read** the typed blueprint, FIC,
and child. Readback verifies relationships, not downstream token issuance or consent.

Turning `AGENT_IDENTITY_ENABLED=true` later is a **separate Azure credential
migration**: first grant the child its actual Azure resource RBAC and validate
token exchange. Empty IDs never fall back to UAMI when that flag is true.

## 3. Add and validate observability

[agent_observability.py](../src/agent_observability.py) uses the recommended
[Microsoft OpenTelemetry distro](https://learn.microsoft.com/microsoft-agent-365/developer/microsoft-opentelemetry),
without the sample's OpenAI Agents `Runner`/user-token hosting layer. The existing
Agent Framework version stays pinned. Modes:

- `off` (default): no SDK initialization, credentials, or export.
- `console`: local metadata spans, no identity requirement, token acquisition,
  network exporter, or invented Entra IDs.
- `agent365`: manual `invoke_agent`, `execute_tool`, and direct AzureOpenAI `chat`
  spans exported under the configured **child Agent ID**, using a dedicated
  `AgentIdentityCredential`, S2S route and observability `/.default` audience.
  Cosmos/Storage can remain on UAMI. Auth failures do not become user-token or
  UAMI-token fallback.

The root invocation and child spans share a generated, request-local conversation
ID. Traces include operation/tool names, duration, status, approval state, and
available direct-chat token counts. They exclude prompts, responses, tool arguments,
user identity, request headers, signed URLs and exception messages/stacks. HTTP,
database, framework auto-instrumentation and application log export are disabled.
The generic Agent Framework chat endpoint currently has root invocation tracing;
automatic framework inference/tool coverage is intentionally not claimed.
These metadata-only traces are **not** full Agent Store/Purview content-validation
compliance or the durable approval audit store. Telemetry never authorizes actions.

After local tests, use the [private build helper](../scripts/build_and_push.py)
on an already-connected runner, with a versioned image tag. No ACR firewall changes
are made. Do not run full `azd up` merely to register an existing agent. The normal
postprovision hooks still do AKS/RBAC setup; they are not a registration-only command.
No build/rollout was performed for this change.

For live verification, use one non-sensitive test invocation, then select the
registered agent's **Activity** in the Microsoft 365 admin center. Per the
[observability concepts](https://learn.microsoft.com/microsoft-agent-365/developer/observability-concepts),
HTTP 200 alone is not proof of ingestion: check rejected-span results and downstream
visibility. No successful ingestion is claimed here. After any rollout, repeat the
[approval test](AGENTS_APPROVAL_FLOW_VALIDATION.md#repeat-the-online-test-intentionally)
with a **new** request and a real human decision; preserve the existing Teams
connection and approval store.

The [advanced managed provisioning guide](AGENTS_STAGED_IDENTITY_DEPLOYMENT.md)
remains available only for teams explicitly choosing to operate directory lifecycle
automation. It is not a prerequisite for the simple onboarding path above.

## Validation and current environment

### September 11 update

- The existing private Teams team is now **Agent Approvals**. Its original IDs
  and Microsoft Workflows installation are unchanged. The Logic App card now
  explicitly approves recommendation generation by `next_best_action`.
  [Live configuration evidence](AGENTS_APPROVAL_FLOW_VALIDATION.md#september-11-branding-and-next-best-action-binding).
- The intended owner's identity was verified in both tenants:
  `christava@microsoft.com`; hosting-tenant user object ID
  `e363bd17-f314-4160-8a2f-86f48d0ba127`, Teams-tenant object ID
  `8d670e18-2717-4de7-9136-7bec806f1f2d`. This verifies the user, **not** ownership
  of an Agent 365 registration that has not been created.
- Foundry account `cog-amotbcj3zss6m` reports `a365LoggingEnabled=true` and
  `a365Status=Enabled` through ARM API `2026-03-15-preview`. The hosting tenant's
  Agent 365 observability service principal exists. Tenant enrollment therefore
  must not be described as wholly absent.
- No Microsoft-managed or tenant-owned **Agent 365 CLI** service principal was
  found. The caller token has no `AgentIdentityBlueprint.*`/`AgentRegistration.*`
  scopes, direct role assignments were empty, and no owned matching blueprint or
  blueprint principal was found. No credentials/permissions were altered.
- The existing Foundry project exposes its own system-assigned managed identity,
  but no Agent ID/blueprint fields were returned. It is not the AKS bootstrap
  UAMI and was not substituted for the requested Next Best Action identity.
- [Foundry built-in integration](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-365-integration)
  covers published prompt/hosted agents. The
  [external custom-asset registration guide](https://learn.microsoft.com/azure/foundry/control-plane/register-custom-agent)
  describes an APIM-backed asset and Application Insights telemetry; it does not
  establish that registering this MCP endpoint issues a UAMI-federated Entra
  Agent ID or grants S2S telemetry permission. No substitute/dummy agent was created.
- ARM reports AKS `aks-amotbcj3zss6m` as **Stopped**. A fresh running-workload,
  token-exchange, telemetry-ingestion and human-approval test remains outstanding.

The remaining blocker for the current AKS integration is an **authorized
onboarding client or existing owner-managed blueprint/Agent ID**, with the
required federation and S2S observability permission. The existing Foundry
enrollment alone does not supply those credentials. No new tenant-admin grants
were attempted, and no approval-disabling/provider-switch flag was added.

### Earlier local implementation validation (September 10)

- Offline regression: **734 tests and 269 subtests passed**, two platform-specific
  skips; real SDK console and S2S serialization/auth routing tested with network
  blocked or HTTP mocked. Focused legacy credential/Foundry tests also passed.
- All three Bicep templates compile; PowerShell/Bash entry points parse.
- No real agent/owner/registration was created by those changes. No live
  Agent 365 export, image build, rollout, or new approval E2E test is claimed.
- The September 10 read-only lookup could not verify availability of the managed Agent
  365 CLI app using existing Azure CLI authentication. No matching active **direct**
  developer/admin role membership was returned; that limited query does not prove
  effective/eligible/group-based access. No login or consent retry was attempted.
- Existing local azd requested flags were **not silently changed**. Apply the
  minimal settings above deliberately after deciding who owns onboarding. The
  last successful live workload remains the UAMI-based approval deployment.