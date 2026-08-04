#!/usr/bin/env python3
"""
End-to-end in-process learning pipeline for the Azure Agents Learning SDK.

This does NOT train model weights, build datasets, or deploy custom models.
Instead it drives the in-process REINFORCE learner that optimizes a small softmax
policy over discrete actions, using the Azure AI Evaluation judges as the reward
signal.

Pipeline:
  1. Generate episodes by sending MHP domain queries to the MCP ask_foundry tool.
  2. Score each new episode with the judges via learning_score_episode.
  3. Initialize a softmax policy over a demo action set via learning_init_policy.
  4. Run one REINFORCE learning batch via learning_run_training.
  5. Print the resulting policy via learning_get_policy.

Usage:
    python scripts/run_learning.py [--port 8000] [--agent-id mcp-agents] [--limit 50]
"""

import json
import re
import sys
import time
import requests
import argparse


# Small demo action set the softmax policy chooses between.
DEMO_ACTIONS = ["concise", "detailed"]


def get_session_url(base_url: str) -> str:
    """Establish SSE session and return the message URL."""
    resp = requests.get(f"{base_url}/sse", stream=True, timeout=15)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            msg_path = line[6:].strip()
            resp.close()
            return f"{base_url}/{msg_path}"
    raise RuntimeError("Failed to obtain SSE session URL")


def mcp_call(base_url: str, tool_name: str, arguments: dict, timeout: int = 120) -> dict:
    """Call an MCP tool and return the parsed result."""
    session_url = get_session_url(base_url)
    resp = requests.post(
        session_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        timeout=timeout,
    )
    result = resp.json()
    content = result.get("result", {}).get("content", [])
    if not content:
        return {"error": f"Empty response: {result}"}
    text = content[0].get("text", "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def list_episode_ids(base_url: str, agent_id: str, limit: int) -> list:
    """Return the ids of the most recent episodes for an agent."""
    data = mcp_call(base_url, "learning_list_episodes", {
        "agent_id": agent_id,
        "limit": limit,
    })
    if "error" in data:
        print(f"  Error listing episodes: {data['error']}")
        return []
    return [ep.get("id") for ep in data.get("episodes", []) if ep.get("id")]


def main():
    parser = argparse.ArgumentParser(description="End-to-end in-process learning pipeline")
    parser.add_argument("--port", type=int, default=8000, help="MCP port (default: 8000)")
    parser.add_argument("--agent-id", default="mcp-agents", help="Agent ID")
    parser.add_argument("--limit", type=int, default=50, help="Max episodes to list/learn from (default: 50)")
    parser.add_argument("--skip-episodes", action="store_true", help="Skip episode generation")
    parser.add_argument("--skip-score", action="store_true", help="Skip judge scoring")
    parser.add_argument("--skip-training", action="store_true", help="Skip policy init + learning batch")
    parser.add_argument("--check-status", type=str, help="Just check a learning run status")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}/runtime/webhooks/mcp"

    # If just checking status
    if args.check_status:
        print(f"Checking learning run {args.check_status}...")
        result = mcp_call(base_url, "learning_get_training_status", {
            "training_run_id": args.check_status,
            "agent_id": args.agent_id,
        })
        print(json.dumps(result, indent=2))
        return 0

    # Load MHP domain queries (existing domain-query generation)
    eval_file = "evals/healthcare_digital_quality/healthcare_digital_quality_eval_data.jsonl"
    queries = []
    try:
        with open(eval_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    queries.append(json.loads(line))
    except FileNotFoundError:
        print(f"ERROR: eval query file not found: {eval_file}")
        return 1

    print(f"Loaded {len(queries)} MHP domain queries")
    print("=" * 70)

    # Snapshot existing episodes so we can score only the new ones later.
    baseline_ids = set(list_episode_ids(base_url, args.agent_id, args.limit))

    # ── Step 1: Generate Episodes via ask_foundry ──
    if not args.skip_episodes:
        print("\n▶ STEP 1: Generating episodes from MHP domain queries (ask_foundry)...")
        success = 0
        for i, item in enumerate(queries, 1):
            query = item["query"]
            print(f"  [{i}/{len(queries)}] {query[:70]}...")
            try:
                result = mcp_call(base_url, "ask_foundry", {
                    "question": query
                }, timeout=180)
                if "error" not in result:
                    print(f"    ✓ Episode captured")
                    success += 1
                else:
                    print(f"    ✗ Error: {str(result.get('error', ''))[:80]}")
            except Exception as e:
                print(f"    ✗ Exception: {e}")
            time.sleep(3)  # cooldown between calls
        print(f"\n  Episodes generated: {success}/{len(queries)}")
    else:
        print("\n▶ STEP 1: Skipping episode generation (--skip-episodes)")

    # ── Step 2: Score new episodes with the Azure AI Evaluation judges ──
    if not args.skip_score:
        print("\n▶ STEP 2: Scoring episodes with the Azure AI Evaluation judges...")
        current_ids = list_episode_ids(base_url, args.agent_id, args.limit)
        new_ids = [eid for eid in current_ids if eid not in baseline_ids]
        # If generation was skipped there are no new ids; score the recent ones.
        target_ids = new_ids if new_ids else current_ids
        print(f"  Scoring {len(target_ids)} episode(s)...")
        scored = 0
        for i, ep_id in enumerate(target_ids, 1):
            result = mcp_call(base_url, "learning_score_episode", {
                "episode_id": ep_id,
                "agent_id": args.agent_id,
            }, timeout=180)
            if result.get("success"):
                agg = result.get("aggregate_reward")
                print(f"  [{i}] ✓ {ep_id[:38]} aggregate_reward={agg}")
                scored += 1
            else:
                print(f"  [{i}] ✗ {ep_id[:38]} - {str(result.get('error', 'unknown'))[:60]}")
        print(f"\n  Scored: {scored}/{len(target_ids)}")
    else:
        print("\n▶ STEP 2: Skipping judge scoring (--skip-score)")

    # ── Steps 3-5: Initialize policy, run learning batch, show policy ──
    if not args.skip_training:
        print(f"\n▶ STEP 3: Initializing softmax policy over actions {DEMO_ACTIONS}...")
        policy_result = mcp_call(base_url, "learning_init_policy", {
            "actions": DEMO_ACTIONS,
            "agent_id": args.agent_id,
        })
        if "error" in policy_result:
            print(f"  Error: {policy_result['error']}")
            return 1
        print(f"  Policy ID: {policy_result.get('policy_id', 'unknown')}")
        print(f"  Actions:   {policy_result.get('actions', DEMO_ACTIONS)}")

        print("\n▶ STEP 4: Running one REINFORCE learning batch...")
        training_result = mcp_call(base_url, "learning_run_training", {
            "agent_id": args.agent_id,
            "limit": args.limit,
            "score_missing": True,
        }, timeout=180)
        if "error" in training_result:
            print(f"  Error: {json.dumps(training_result, indent=2)}")
            return 1
        training_id = training_result.get("training_run_id", "unknown")
        status = training_result.get("status", "unknown")
        print(f"  Training Run ID: {training_id}")
        print(f"  Status:          {status}")
        print(f"  Episodes used:   {training_result.get('episodes_used', 0)}")
        print(f"  Metrics:         {json.dumps(training_result.get('metrics', {}))}")

        # ── Step 5: Print the resulting policy ──
        print("\n▶ STEP 5: Resulting policy...")
        policy = mcp_call(base_url, "learning_get_policy", {
            "agent_id": args.agent_id,
        })
        if policy.get("has_policy"):
            print(f"  Policy ID:       {policy.get('policy_id')}")
            print(f"  Version:         {policy.get('version')}")
            print(f"  Action probs:    {json.dumps(policy.get('action_probabilities', {}))}")
            print(f"  Episodes seen:   {policy.get('episodes_seen')}")
            print(f"  Updates applied: {policy.get('updates_applied')}")
        else:
            print(f"  {policy.get('message', policy.get('error', 'No policy available'))}")

        print(f"\n  Check the learning run status later with:")
        print(f"    python scripts/run_learning.py --check-status {training_id}")
    else:
        print("\n▶ STEPS 3-5: Skipping policy init, learning, and readout (--skip-training)")

    print("\n" + "=" * 70)
    print("Pipeline complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
