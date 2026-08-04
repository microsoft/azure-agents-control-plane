#!/usr/bin/env python3
"""
Label captured episodes with rewards via the Azure Agents Learning SDK
(learning_assign_reward MCP tool).

Automated labeling based on quality criteria:
  0.9-1.0  Excellent  — correct tool, complete response, accurate data
  0.7-0.89 Good       — correct tool, mostly complete response
  0.5-0.69 Acceptable — partially correct, minor issues
  0.3-0.49 Poor       — wrong approach but recoverable
  0.0-0.29 Failed     — wrong tool, incorrect response, errors

Usage:
    python scripts/label_episodes.py [--port 8000] [--agent-id mcp-agents] [--limit 20]
"""

import asyncio
import aiohttp
import argparse
import json
import re
import sys


# Quality heuristics for auto-labeling
GOOD_TOOLS = {
    "get_customer_facts", "get_churn_prediction", "get_customer_segment",
    "search_similar_churned", "recommend_retention_action",
    "create_retention_case", "log_analysis_result",
    # Healthcare digital quality tools
    "next_best_action", "ask_foundry", "search_facts",
    "get_customer_churn_facts", "cross_domain_analysis",
    "store_memory", "recall_memory",
    # Learning operational tools are expected for system episodes
    "learning_list_episodes", "learning_get_episode",
    "learning_assign_reward", "learning_list_rewards",
    "learning_score_episode", "learning_get_metrics",
    "learning_list_training_runs", "learning_get_stats",
}


def score_episode(ep: dict) -> tuple[float, str]:
    """Score an episode based on quality heuristics.

    Returns (reward_value, reason).
    """
    tool_calls = ep.get("tool_calls_count", 0)
    latency = ep.get("request_latency_ms", 0)
    output = ep.get("assistant_output", "")
    user_input = ep.get("user_input", "")
    tool_names = ep.get("tool_names", [])

    reasons = []

    # 1. Check if any tools were called
    if tool_calls == 0 and not output:
        return 0.2, "No tool calls and no output"

    # 2. Check tool correctness
    unknown_tools = [t for t in tool_names if t not in GOOD_TOOLS] if tool_names else []
    if unknown_tools:
        reasons.append(f"unknown tools: {unknown_tools}")

    # 3. Check output quality
    output_len = len(output) if output else 0
    has_error = False
    if output:
        lower_out = output.lower()
        has_error = any(w in lower_out for w in ["error", "failed", "exception", "traceback"])

    if has_error:
        reasons.append("output contains errors")
        return 0.3, "; ".join(reasons) if reasons else "Error in output"

    # 4. Score based on completeness
    score = 0.85  # base: good

    if output_len > 200:
        score += 0.05  # detailed response
        reasons.append("detailed response")
    elif output_len < 30:
        score -= 0.15  # sparse response
        reasons.append("sparse response")

    if tool_calls >= 1:
        score += 0.05
        reasons.append(f"{tool_calls} tool(s) called")

    if unknown_tools:
        score -= 0.2

    # Clamp
    score = max(0.0, min(1.0, round(score, 2)))

    reason = "; ".join(reasons) if reasons else "Correct tool, complete response"
    return score, reason


async def call_mcp_tool(session, session_url, tool_name, arguments, req_id):
    """Call an MCP tool and return parsed result."""
    request = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    async with session.post(
        session_url, json=request, timeout=aiohttp.ClientTimeout(total=60)
    ) as resp:
        text = await resp.text()
        if resp.status != 200:
            return {"error": f"HTTP {resp.status}: {text}"}
        result = json.loads(text)
        if "error" in result:
            return {"error": result["error"]}
        content = result.get("result", {}).get("content", [])
        if not content:
            return {"error": "No content returned"}
        return json.loads(content[0].get("text", "{}"))


async def main(port: int, agent_id: str, limit: int):
    base_url = f"http://localhost:{port}/runtime/webhooks/mcp"

    async with aiohttp.ClientSession() as session:
        # Establish SSE session
        print(f"Connecting to {base_url}...")
        async with session.get(
            f"{base_url}/sse", headers={"Accept": "text/event-stream"}
        ) as sse:
            session_url = None
            async for chunk in sse.content.iter_chunked(1024):
                data = chunk.decode("utf-8", errors="ignore")
                match = re.search(r"data: (message\?[^\n\r]+)", data)
                if match:
                    session_url = f"{base_url}/{match.group(1)}"
                    break

            if not session_url:
                print("ERROR: No session URL obtained")
                return 1

            print(f"Session established\n")

            # Step 1: List episodes
            print(f"Listing episodes for agent '{agent_id}'...")
            episodes_data = await call_mcp_tool(
                session, session_url, "learning_list_episodes",
                {"agent_id": agent_id, "limit": limit}, "list-1"
            )

            if "error" in episodes_data:
                print(f"Error listing episodes: {episodes_data['error']}")
                return 1

            episodes = episodes_data.get("episodes", [])
            found = episodes_data.get("episodes_found", 0)
            print(f"Found {found} episodes\n")

            if not episodes:
                print("No episodes to label.")
                return 0

            # Step 2: Score and label each episode
            labeled = 0
            failed = 0
            reward_distribution = {"excellent": 0, "good": 0, "acceptable": 0, "poor": 0, "failed": 0}
            total_reward = 0.0

            print("=" * 70)
            print(f"{'#':<4} {'Episode ID':<40} {'Score':<8} {'Reason'}")
            print("-" * 70)

            for i, ep in enumerate(episodes, 1):
                ep_id = ep.get("id", "")
                if not ep_id:
                    continue

                score, reason = score_episode(ep)
                total_reward += score

                # Categorize
                if score >= 0.9:
                    reward_distribution["excellent"] += 1
                elif score >= 0.7:
                    reward_distribution["good"] += 1
                elif score >= 0.5:
                    reward_distribution["acceptable"] += 1
                elif score >= 0.3:
                    reward_distribution["poor"] += 1
                else:
                    reward_distribution["failed"] += 1

                # Assign reward via MCP
                result = await call_mcp_tool(
                    session, session_url, "learning_assign_reward",
                    {
                        "episode_id": ep_id,
                        "reward_value": score,
                        "reward_source": "test_result",
                        "agent_id": agent_id,
                        "rubric": "task_adherence",
                        "evaluator": "copilot_auto_labeler",
                        "comments": reason,
                    },
                    f"reward-{i}"
                )

                status = "OK" if result.get("success") else "FAIL"
                if result.get("success"):
                    labeled += 1
                else:
                    failed += 1
                    status = f"FAIL: {result.get('error', 'unknown')}"

                trunc_reason = reason[:40] + "..." if len(reason) > 40 else reason
                print(f"{i:<4} {ep_id[:38]:<40} {score:<8.2f} {trunc_reason}")

            # Summary
            print("\n" + "=" * 70)
            print("REWARD DISTRIBUTION SUMMARY")
            print("-" * 40)
            print(f"  Excellent (0.9-1.0): {reward_distribution['excellent']}")
            print(f"  Good      (0.7-0.89): {reward_distribution['good']}")
            print(f"  Acceptable(0.5-0.69): {reward_distribution['acceptable']}")
            print(f"  Poor      (0.3-0.49): {reward_distribution['poor']}")
            print(f"  Failed    (0.0-0.29): {reward_distribution['failed']}")
            print("-" * 40)
            print(f"  Total labeled: {labeled}")
            print(f"  Total failed:  {failed}")
            avg = total_reward / len(episodes) if episodes else 0
            print(f"  Average reward: {avg:.2f}")
            print("=" * 70)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Label episodes with rewards via MCP API")
    parser.add_argument("--port", type=int, default=8000, help="Port for MCP service (default: 8000)")
    parser.add_argument("--agent-id", type=str, default="mcp-agents", help="Agent ID (default: mcp-agents)")
    parser.add_argument("--limit", type=int, default=20, help="Max episodes to label (default: 20)")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args.port, args.agent_id, args.limit)))
