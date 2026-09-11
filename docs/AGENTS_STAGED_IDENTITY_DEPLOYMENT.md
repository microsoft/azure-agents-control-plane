# Advanced: repository-managed Agent ID deployment

> **Not the default onboarding path.** For registration, owner/sponsor, and
> telemetry without UAMI directory-management grants, use the
> [minimal Agent 365 guide](AGENTS_AGENT365_ONBOARDING.md). This advanced workflow
> requires the explicit `AGENT_IDENTITY_PROVISIONING_MODE=managed` opt-in.

Use this path to enable Agent ID in an **existing** environment without
reprovisioning AKS, ACR, Logic Apps, Teams connections, databases, or approval
records. `AGENT_IDENTITY_ENABLED=true` is a request, not evidence that an identity
exists. Missing/partial IDs remain a hard error; there is no UAMI fallback when
Agent ID is enabled and no deferred-success registry publication.

## Trust boundaries and current status

- **Stage 1:** [deploy_agent_identity.py](../scripts/deploy_agent_identity.py)
  invokes only [agent-identity.bicep](../infra/agent-identity.bicep). The template
  uses the existing configuration UAMI. Blueprint mode reconciles directory
  blueprint/principal/federation; identity mode additionally creates/adopts the
  child and assigns its Azure roles on existing Cosmos, Storage, Foundry, and
  optional Search resources. Those are role-assignment writes, not data writes.
  Azure Deployment Scripts may create their temporary backing resources.
- **Stage 2:** [deployment_gate.py](../scripts/deployment_gate.py) checks actual
  UAMI/blueprint Graph grants **in managed mode**, the live child-to-blueprint relationship, blueprint
  federation, registry settings/consent, and saved publication journal. The
  [Windows hook](../infra/hooks/postprovision.ps1) and
  [Linux hook](../infra/hooks/postprovision.sh) run it before setup, and again
  immediately before approval configuration/rollout. The build helper also
  gates direct builds. Publication repeats the gate after successful rollout.
- No script logs in, grants **Graph consent**, elevates a directory role, or
  changes Conditional Access. A denied or inconclusive preflight stops work.
  Run the write phases only from an administrator-approved device or deployment
  runner using an appropriately authorized existing login.
- Identity-only checks do not prove downstream token issuance, Azure RBAC
  propagation, private DNS, or resource health. Verify those from the workload
  after deployment before declaring the migration complete.

At the last live check on 2026-09-09, the `dev` UAMI lacked its seven Graph roles,
no blueprint IDs had been resolved, and the publisher lacked registry consent.
The Graph grant attempt was denied (403), and device-code consent was blocked
by the Microsoft Non-Production managed-device policy. **This implementation
does not remove those external prerequisites.** The earlier
[approval validation](AGENTS_APPROVAL_FLOW_VALIDATION.md) used the UAMI runtime,
not the new identity-enabled deployment. No new live rollout is claimed here.

## Administrator prerequisites (managed mode only, outside deployment)

Use the hosting tenant, not the separate Teams approver tenant. Azure RBAC Owner
does not confer permission to consent Microsoft Graph roles.

| Principal | Required Graph application permissions |
| --- | --- |
| Configuration UAMI | `AgentIdentityBlueprint.Create`, `AgentIdentityBlueprint.Read.All`, `AgentIdentityBlueprint.AddRemoveCreds.All`, `AgentIdentityBlueprint.UpdateBranding.All`, `AgentIdentityBlueprint.UpdateAuthProperties.All`, `AgentIdentityBlueprintPrincipal.Create`, `AgentIdentityBlueprintPrincipal.Read.All` |
| Blueprint **service principal**, after it exists | `AgentIdentity.CreateAsManager`, `AgentIdentity.Read.All` for child creation/discovery |
| Separate registry deployment caller | `AgentRegistration.Read.All`, `AgentRegistration.ReadWrite.All` (application or supported delegated consent) |

Directory inspection also requires an authorized reader: see the prerequisites
in [agent_identity_preflight.py](../scripts/agent_identity_preflight.py).
Precreated-child adoption uses the documented read/manager alternatives rather
than assuming child-creation permission. Never grant registry publishing roles
to the child runtime identity.

For `dev`, the last verified configuration UAMI was `id-mcp-amotbcj3zss6m`,
object ID `be374404-0a6a-4843-b71a-64f36d87bfcf`, in tenant
`16b3c013-d300-468d-ac64-7eda0820b6d3`. A compliant administrator must resolve
the missing consent; repeated device-code login is not a remedy.

## Stage 1: provision identity, then export verified identifiers

Run from the repository root with Python 3.10+, Azure CLI, azd, and the deployment
Python dependencies installed. `dev` must already contain subscription/tenant,
resource-group, UAMI client ID, sponsor user ID, and existing service settings.
The coordinator reads the **named** azd environment; it does not overlay stale
process identity settings or change the selected Azure subscription.

```powershell
# Desired flags only; these do not create directory objects.
azd env set AGENT_IDENTITY_PROVISIONING_MODE managed --environment dev
azd env set AGENT_IDENTITY_ENABLED true --environment dev
azd env set AGENT_REGISTRY_ENABLED true --environment dev
azd env set AGENT_REGISTRY_API agent365 --environment dev

# After the administrator grants the configuration UAMI's roles:
python scripts/deploy_agent_identity.py --environment dev --phase blueprint
if ($LASTEXITCODE -ne 0) { throw 'Blueprint prerequisites are blocked.' }
python scripts/deploy_agent_identity.py --environment dev --phase blueprint --apply
if ($LASTEXITCODE -ne 0) { throw 'Stop and reconcile the scoped blueprint deployment.' }
```

Set `AGENT_SPONSOR_PRINCIPAL_ID` and `AGENT_REGISTRY_OWNER_IDS` to approved
**hosting-tenant** object IDs before these commands; they are never inferred from
the Teams approver allowlist. Blueprint defaults match the existing `id-mcp-*`
deployment suffix. Custom UAMIs require `AGENT_BLUEPRINT_UNIQUE_NAME`; custom
display names can use `AGENT_BLUEPRINT_DISPLAY_NAME`/`AGENT_IDENTITY_DISPLAY_NAME`.

Blueprint bootstrap is an internal pause within stage 1: its new principal
cannot be consented before it exists. An administrator now consents the two
child-lifecycle roles to the returned **blueprint principal**. Then:

```powershell
python scripts/deploy_agent_identity.py --environment dev --phase identity
if ($LASTEXITCODE -ne 0) { throw 'Blueprint principal consent is not ready.' }
python scripts/deploy_agent_identity.py --environment dev --phase identity --apply
if ($LASTEXITCODE -ne 0) { throw 'Stop and reconcile the scoped identity deployment.' }
```

The coordinator validates all outputs, rereads directory relationships/trust,
and only then saves allowlisted, nonempty values with `azd env set`. It preserves
the requested flags and all unrelated azd settings. In particular:

| Output | Meaning |
| --- | --- |
| `AGENT_IDENTITY_BLUEPRINT_APP_ID` | Blueprint application/client ID, used in token exchange |
| `AGENT_IDENTITY_BLUEPRINT_OBJECT_ID` | Blueprint **application object** ID, used in registry metadata |
| `AGENT_IDENTITY_BLUEPRINT_PRINCIPAL_ID` | Blueprint **service-principal object** ID, used for Graph consent |
| `AGENT_IDENTITY_APP_ID` / `AGENT_IDENTITY_PRINCIPAL_ID` | Child Agent ID; these have the same value, not the UAMI's IDs |

The saved `EXISTING_AGENT_IDENTITY_BLUEPRINT_APP_ID` and
`EXISTING_AGENT_IDENTITY_ID` also let the parent template adopt these objects on
future infrastructure deployments. Explicit overrides are accepted via
`--blueprint-app-id` and `--agent-identity-id`; conflicting saved IDs stop work.

Deployments are named `agent-id-blueprint-<environment>` and
`agent-id-identity-<environment>`. Serialize operators/runners for each environment.
After an uncertain apply or interrupted azd export, inspect that same deployment
and run the corresponding phase with `--import-outputs`. This reads a Succeeded
deployment and revalidates its outputs; it never replays provisioning. Do not
reset IDs, delete registry journals, or retry ambiguous Graph creation blindly.

## Stage 2: gate, build privately, deploy by digest, publish

The [build helper](../scripts/build_and_push.py) never runs ACR update or
network-rule commands, on success **or** failure. It verifies network policy
before/after building and stops on external drift without overwriting it.

Select one already-authorized network path:

- `ACR_BUILD_MODE=acr-task` plus `ACR_TASK_AGENT_POOL=<existing pool>` for a
  restricted registry. The pool must be provisioned, Linux, have at least one
  instance, and be VNet-attached. The submitting machine also needs access to
  upload the local build context and read logs. No pool is created automatically.
- `ACR_BUILD_MODE=docker` on a runner with Docker, private DNS/routing to the ACR,
  approved package-feed access and push authorization. Existing Azure CLI login
  is used for ACR authentication; TLS checks are not weakened.

See [Microsoft's dedicated ACR pool guidance](https://learn.microsoft.com/azure/container-registry/tasks-agent-pools).
Pool placement alone is not a connectivity test. Missing network prerequisites
remain a blocker, not a reason to temporarily open public access.

```powershell
azd env set IMAGE_TAG agent-id-20260909-01 --environment dev
# Example only: use an already-connected Docker runner.
azd env set ACR_BUILD_MODE docker --environment dev

python scripts/deployment_gate.py --environment dev --check
if ($LASTEXITCODE -ne 0) { throw 'Identity or registry gate is blocked.' }
python scripts/build_and_push.py --environment dev --check-only
if ($LASTEXITCODE -ne 0) { throw 'Build prerequisites are blocked.' }

# On the approved runner, use existing postprovision after stage 1, not azd up.
$env:AZURE_ENV_NAME = 'dev'
./infra/hooks/postprovision.ps1
```

On Linux, use the corresponding Bash postprovision hook. Build wrappers select
the workspace virtual environment if present, or `DEPLOYMENT_PYTHON` when set.
`IMAGE_TAG` must be explicit/versioned for Agent ID; hooks keep the same tag in
the trusted approval context, but deploy the verified immutable **digest**.

The existing postprovision hooks still perform their normal AKS setup and Azure
RBAC operations **after** the gate; they are not read-only and are not part of
the identity-only template. The identity-only phase never executes them.

Registry publication occurs after rollout using the same named environment,
owners, source key, optional managing deployment app, and durable journal.
`AZURE_ENV_NAME` remains the source/journal scope even if
`DEPLOYMENT_ENVIRONMENT` has a different runtime label. Keep/share the journal
across runners. A pending journal blocks before building; recover its registration
ID rather than sending another POST. Registry errors remain fatal and are not
reported as a successful enrollment.

## Approval retest and validation limits

Preserve the existing dedicated callback application, Logic App system identity,
authorized Teams OAuth connection, approver allowlist and approval Cosmos data.
Only the agent's downstream credential changes. Do not change image/commit/task
context between initiating a new test request and resuming it.

After verifying workload token acquisition as the child Agent ID, follow the
[online approval test procedure](AGENTS_APPROVAL_FLOW_VALIDATION.md#repeat-the-online-test-intentionally):
read-only MCP preflight, one synthetic request, real human decision in Teams,
authenticated callback, same-state resume. Never automate the human approval or
reuse the expired approval from the earlier validation report.

The new behavior is covered by
[offline staged deployment tests](../tests/test_staged_identity_deployment_unit.py).
They mock every CLI/Graph boundary and exercise blocked permissions, empty or
wrong IDs, bootstrap-vs-ready status, journal recovery, failed private builds and
network drift. Bicep compilation and these tests are not live deployment evidence.

Local validation for this change: **689 tests and 269 subtests passed**, two
platform-specific skips. The scoped identity, parent, and scoped approval
templates compile; PowerShell and LF-normalized Bash entry points parse. The
named `dev` gate and build `--check-only` both returned blocked/exit 2 on the empty
blueprint ID before any Graph call, registry authentication, build, or rollout.
No consent retry, permission assignment, identity deployment or workload change
was performed during this implementation.