"""Compose the tracked Next Best Action Logic App and Teams assets offline.

Mirrors the Bicep union: no rendering/evaluation of WDL expressions, networking,
secrets or deployment. The scaffold alone is not a deployable workflow because
the tracked Teams request and response-received text must be composed into it.
"""

import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "agent-approvals"


def load_approval_workflow() -> dict:
    workflow = json.loads((ASSETS / "workflows/agent_approval_logic_app.json").read_text(encoding="utf-8"))
    card = json.loads((ASSETS / "teams/agent_approval_card.json").read_text(encoding="utf-8"))
    acknowledgement = json.loads((ASSETS / "teams/agent_approval_result_card.json").read_text(encoding="utf-8"))
    actions = workflow["actions"]["Collect_Decision"]["actions"]
    actions["Build_Adaptive_Card"]["inputs"] = card
    actions["Wait_for_Teams_Response"]["inputs"]["body"]["body"]["updateMessage"] = acknowledgement["body"][0]["text"]
    return workflow