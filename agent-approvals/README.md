# Agent Approvals — Next Best Action

This directory contains the **custom Logic App/Teams approval transport** for the
`next_best_action` MCP tool. It is not a native Microsoft Agent 365 approval API.
The former directory name was misleading; genuine Agent 365 registry/identity/
observability integrations keep their product/API names elsewhere in the code.

## Approval scope

1. A caller requests a Next Best Action recommendation.
2. The Python checkpoint detects deployment/CI-CD-related requests. It persists
   exact task, requester and deployment context, approvers, hash and expiry in Cosmos.
3. The Logic App posts an **Agent Approvals** card in the dedicated Teams channel.
4. A configured human decides. The Logic App sends a signed callback to Python.
5. Python validates identity, request hash, expiry, dispatch and concurrency before
   persisting the decision. Only the same request can resume.

**Approval permits recommendation generation, not execution of the resulting
plan or a real deployment.** Other recommendations retain the existing policy;
this rename does not silently require approval for every tool or healthcare action.

## Source of truth

| Artifact | Purpose |
| --- | --- |
| [teams/agent_approval_card.json](teams/agent_approval_card.json) | Canonical request card: Next Best Action, exact context, `Action.Submit`, decision/comment only |
| [teams/agent_approval_result_card.json](teams/agent_approval_result_card.json) | Response-received acknowledgement; its first text block supplies the connector's `updateMessage` |
| [workflows/agent_approval_logic_app.json](workflows/agent_approval_logic_app.json) | Workflow scaffold: validation, Teams response, authenticated callback, fail-closed errors |
| [../scripts/approval_assets.py](../scripts/approval_assets.py) | Offline Python composition of scaffold and Teams assets |
| [../infra/app/agents-approval-logicapp.bicep](../infra/app/agents-approval-logicapp.bicep) | Composes the same artifacts when provisioning the workflow |
| [../src/agent365_approval.py](../src/agent365_approval.py) | Durable Python authorization engine; module name retained for compatibility, not a claim of native Agent 365 approvals |
| [manifests/agent_instance.json](manifests/agent_instance.json), [manifests/agent_card_manifest.json](manifests/agent_card_manifest.json) | Optional Agent 365 registration metadata only; neither file grants approval |

**Do not deploy the workflow scaffold directly.** Bicep or the Python composer
must insert the canonical Teams card and acknowledgement. This removes the old
unused bot-specific `Action.Execute` card examples. No bot handlers are implied.

## Teams application versus team

The dedicated team is named **Agent Approvals** (formerly **Agents365 Approvals**).
Its channel remains **Approvals**, a standard channel inside a private team.
The installed application is Microsoft's **Workflows**. There is no custom Teams
app package to rename, publish, or sideload. The reusable Teams code is the two
versioned card files above; see [teams/README.md](teams/README.md).

Team/channel IDs, membership, authorized Teams OAuth connection, Logic App system
identity, callback app/audience and Cosmos approval data must be preserved on rename.
The first channel response completes the card, so membership remains restricted.

## Agent 365 activation and future approval-provider switch

Registration, owner/sponsor, Entra Agent ID and observability are independent of
this transport. Follow [the onboarding guide](../docs/AGENTS_AGENT365_ONBOARDING.md).
An actual Agent ID can federate to the existing UAMI without granting that UAMI
tenant-wide blueprint-management permissions. Creation/ownership, client consent
and S2S telemetry authorization must nevertheless already be permitted.

Keep Logic App approval enabled until **all** of the following are verified live:

- Agent 365 registration with the intended human owner/sponsor.
- Genuine Agent ID, blueprint/UAMI trust and authenticated telemetry under that ID.
- Accepted invocation/tool activity in the registered agent's Activity view.
- A documented alternative approval service with authenticated human decisions,
  request binding, expiry, durable replay-safe state and equivalent fail-closed tests.

No Agent 365 approval provider or disabling feature flag is implemented by this
rename. The existing `APPROVAL_LOGIC_APP_ENABLED` controls infrastructure provisioning;
it is **not** a safe runtime bypass and does not authorize pending requests.
Agent registration approval/admin consent is not approval of runtime actions.

See [the live validation report](../docs/AGENTS_APPROVAL_FLOW_VALIDATION.md) for
the historical successful human approval. Use a **new** request for retesting.