# Teams code for Agent Approvals

The actual Teams transport is the Microsoft **Workflows** connector operation
`PostCardAndWaitForResponse` (`ApiConnectionWebhook` in Logic Apps). No custom
bot, app manifest, installable ZIP, or `Action.Execute` handler is deployed.

## Versioned artifacts used in deployment

- [agent_approval_card.json](agent_approval_card.json) is the canonical card for
  the `next_best_action` recommendation checkpoint. Its WDL expressions are
  evaluated by the Logic App when the card is composed. It sends only `decision`
  and the `comment` input; human identity comes from the connector response envelope.
- [agent_approval_result_card.json](agent_approval_result_card.json) defines a
  response-received acknowledgement. The first text block is consumed as the
  connector's plain-text `updateMessage`; it is not posted as a second result
  card and does not claim that Python has accepted the decision.

Both files are loaded by [the Bicep module](../../infra/app/agents-approval-logicapp.bicep)
and [the offline composer](../../scripts/approval_assets.py). Editing the request
card therefore changes the deployed card, not a disconnected example.

Branding: **Agent Approvals** team, **Approvals** channel, **Next Best Action**
use case. Approve means "allow a recommendation for this bound request," not
"execute a deployment." Keep the existing team/channel IDs and private membership.
Microsoft Workflows retains its application name and ID.

Never check in signed workflow URLs, tokens, generated environment files or
connection credentials. The full contract is in [../README.md](../README.md).