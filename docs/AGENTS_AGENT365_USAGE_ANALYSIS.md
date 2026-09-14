# Agent 365 usage in the Next Best Action agent

**Assessment date:** 2026-09-10

**Last verified live approval deployment:** 2026-09-09

> **September 11 update:** the existing team was renamed to **Agent Approvals**
> and the live Logic App now uses the checked-in Next Best Action recommendation
> card. The team/channel IDs, Workflows app, OAuth connection and approval state
> were preserved. ARM reports AKS as **Stopped**, so no new runtime or human
> approval test is claimed. Foundry reports Agent 365 enabled, but a registered,
> owner-bound and UAMI-federated Agent ID for this AKS agent is still outstanding.
> See [the current onboarding status](AGENTS_AGENT365_ONBOARDING.md#september-11-update)
> and [the live configuration evidence](AGENTS_APPROVAL_FLOW_VALIDATION.md#september-11-branding-and-next-best-action-binding).

## Bottom line

**Agent 365 is not yet active in the last verified live deployment.** The Next
Best Action agent runs on AKS using Azure managed identity. Its working
human-approval flow is **custom Python + Cosmos DB + Logic Apps + Teams**, not
an Agent 365 approval service.

Agent 365 integration is implemented in the source, but onboarding and deployment
remain incomplete. At the September 10 assessment, the adoption and observability
changes were local and uncommitted. The September 11 update above records the
subsequent live transport changes separately; it does not claim that identity or
observability activation has been verified for the AKS agent.

## Capability summary

| Capability | How it fits into Next Best Action | Actual status |
| --- | --- | --- |
| **Registration and ownership** | Makes the agent discoverable and governable in Agent 365, with an accountable human owner/sponsor. Registration is metadata, not something consulted for every recommendation. | **Not completed.** No verified registration or assigned owner for this agent. Standard Agent 365 onboarding is the recommended path; the repository publisher is an optional alternative. |
| **Microsoft Entra Agent ID** | Gives the agent its own identity for authenticated service calls. The implementation exchanges UAMI credentials through the blueprint to obtain a child-agent token. | **Implemented, not active live.** No real blueprint/child IDs are configured. Existing Azure resource access remains on UAMI. |
| **Agent 365 observability** | Exports agent invocation, tool execution, direct inference, token-usage and approval-state metadata using authenticated S2S export. | **Implemented locally, currently off.** No live ingestion verified. The new trace integration excludes prompts, arguments, responses, tokens and raw errors. |
| **Human approvals** | Blocks deployment-related planning until a durable, matching, unexpired approval has been verified. | **Live and previously validated, but custom—not Agent 365-native.** |
| **Agent 365 messaging, notifications and Work IQ tools** | Would provide capabilities such as an agent mailbox, Teams conversational presence or Microsoft 365 data access. | **Not used.** The Teams Workflows approval card does not provide these capabilities. |

## What actually happens in the verified deployment

For a deployment-related request:

1. The Python agent checks the approval policy before planning.
2. It stores an approval request in Cosmos DB and invokes the Logic App.
3. The Logic App posts an approval card through Teams Workflows.
4. A human decides; an authenticated callback records the result.
5. The same request can resume and generate a recommendation only after the
   approval, validation, expiry and request-context checks pass.

The successful live test returned a recommendation. **It did not execute the
plan, forge the callback, or automate the human decision.** The historical test
approval has expired and must not be reused.

Neither Agent 365 registration nor its telemetry service makes the approval
decision. The names of the approval module and Teams team do not mean an Agent
365 approval API is involved.

Evidence: [AGENTS_APPROVAL_FLOW_VALIDATION.md](AGENTS_APPROVAL_FLOW_VALIDATION.md).

## What is implemented in source

### Identity

[../src/agent_identity.py](../src/agent_identity.py) provides
`AgentIdentityCredential` for the secretless UAMI → blueprint → child Agent ID
token exchange. When `AGENT_IDENTITY_ENABLED=true`, missing identity settings
fail rather than silently falling back to UAMI.

Normal deployment now defaults to `AGENT_IDENTITY_PROVISIONING_MODE=adopt`.
It consumes already-provisioned identities instead of creating and reconciling
directory objects. The deployment check verifies existing identity relationships
and federation without requiring blueprint-management Graph roles on the UAMI.
The advanced custom provisioning workflow requires an explicit `managed` opt-in.

### Registration

[../src/agent_registry.py](../src/agent_registry.py) implements an optional,
deployment-time metadata publisher. It does not issue identities, authorize
recommendations, or run on each agent request.

If standard Agent 365 onboarding already registered the agent, leave the
repository publisher disabled to avoid duplicate or incompatible registrations.
Registry IDs, blueprint IDs, child Agent IDs and UAMI IDs are not interchangeable.

### Observability

[../src/agent_observability.py](../src/agent_observability.py) uses the official
Microsoft OpenTelemetry distro. It supports:

- `off`: no telemetry SDK initialization or export.
- `console`: local metadata traces, without identity requirements or network export.
- `agent365`: authenticated S2S export using the configured child Agent ID and
  the observability resource's `/.default` audience.

The wiring in [../src/next_best_action_agent.py](../src/next_best_action_agent.py)
adds root invocation spans, MCP tool spans, direct AzureOpenAI chat spans,
available token counts, and approval-state metadata. Generic Agent Framework
chat endpoints have root invocation tracing; full automatic framework
inference/tool coverage is not claimed.

Automatic instrumentation and application log export are disabled in this
integration. Metadata-only telemetry is not the durable approval audit store,
nor proof of full Agent Store or Purview content-validation compliance.

Telemetry has its own Agent ID credential. **Cosmos, Storage and approvals can
continue using the existing UAMI while Agent 365 telemetry uses the onboarded
child identity.** These are separate configuration choices.

## Local configuration versus live state

The latest local azd `dev` settings check returned:

| Setting | Local requested value |
| --- | --- |
| `MCP_AGENT_RUNTIME` | `python` |
| `APPROVAL_LOGIC_APP_ENABLED` | `true` |
| `AGENT_IDENTITY_ENABLED` | `true` |
| `AGENT_IDENTITY_PROVISIONING_MODE` | `adopt` |
| `AGENT_REGISTRY_ENABLED` | `true` |
| `AGENT_REGISTRY_API` | `agent365` |
| `AGENT_OBSERVABILITY_MODE` | `off` |

`AGENT_IDENTITY_APP_ID`, `AGENT_IDENTITY_BLUEPRINT_APP_ID`,
`AGENT_IDENTITY_BLUEPRINT_OBJECT_ID` and `AGENT_REGISTRY_ID` were all empty.
**Requested flags do not establish successful onboarding or deployment.**

The last verified live workload still has Agent Identity disabled and uses UAMI.
The current [deployment gate](../scripts/deployment_gate.py#L291) blocks the
requested identity-enabled build/rollout while the required IDs are missing.

## Simplified target and remaining work

The target is **register once, verify owner/sponsor and Agent ID, then enable
telemetry—without changing the working Azure resource credentials or approval
transport.**

1. Complete authorized standard Agent 365 onboarding, or obtain a real identity
   from an approved platform team.
2. Verify the human owner/sponsor and registration, then adopt the actual IDs.
3. Configure the blueprint's federation trust to the hosting UAMI and the
   required S2S observability permission and tenant licensing.
4. Keep Azure data-plane credentials on UAMI and avoid running a second registry
   publisher when onboarding owns the registration.
5. Validate local traces, build and deploy through the existing private network
   path, then verify telemetry in the agent's Activity view.
6. Retest approvals with a new request and a real human decision after rollout.

For an administrator-enabled target tenant, these steps are now packaged by
[`install-agent365.ps1`](../scripts/install-agent365.ps1). Its default mode is a
read-only Agent 365 CLI preview; `-Apply` performs official S2S onboarding and
verified `azd` adoption, while `-Apply -Deploy` continues through the existing
gated build and AKS rollout. It requires `christava@microsoft.com` to be the
signed-in setup user and proves blueprint/Agent ID/registration ownership, UAMI
federation, the observability application role and an assigned eligible license
before enabling Agent 365 telemetry. This automation has offline test coverage
but has not been run against the blocked Non-Production tenant.

The seven UAMI directory-management permissions belong to the custom provisioning
automation, not ordinary use of an onboarded identity. Tenant-authorized identity
creation, applicable consent, and Conditional Access still apply. HTTP 200 from
a telemetry endpoint alone is not proof that the tenant accepted the spans.

Implementation guidance: [AGENTS_AGENT365_ONBOARDING.md](AGENTS_AGENT365_ONBOARDING.md).

## Validation limits

- The administrator installer/importer has 27 offline tests. The focused Agent
   365 identity, registry, observability, deployment and NBA wiring suite passes
   **317 tests** after this packaging work.
- The named offline regression suite passed **734 tests and 269 subtests**, with
  two platform-specific skips. Bicep compilation and shell syntax checks passed.
- Real SDK console behavior and S2S export formatting/auth routing were tested
  with network access blocked or HTTP mocked—not against live Agent 365 ingestion.
- Actual registration, owner assignment, Agent ID issuance, live telemetry,
  identity/telemetry-enabled redeployment and a new approval E2E test remain
  incomplete. No cloud permission changes were made during the simplification.