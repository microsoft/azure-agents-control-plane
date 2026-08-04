#!/usr/bin/env python3
"""
List captured episodes from the autonomous-agent via MCP API.

Usage:
    python scripts/list_episodes.py [--port 8000] [--agent-id mcp-agents] [--limit 10]
"""

import asyncio
import aiohttp
import argparse
import json
import re
import sys


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

            # Call learning_list_episodes tool
            request = {
                "jsonrpc": "2.0",
                "id": "list-episodes-1",
                "method": "tools/call",
                "params": {
                    "name": "learning_list_episodes",
                    "arguments": {
                        "agent_id": agent_id,
                        "limit": limit,
                    },
                },
            }

            async with session.post(
                session_url,
                json=request,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    print(f"HTTP {resp.status}: {text}")
                    return 1

                result = json.loads(text)
                if "error" in result:
                    print(f"Error: {result['error']}")
                    return 1

                content = result.get("result", {}).get("content", [])
                if not content:
                    print("No content returned")
                    return 1

                episodes_json = json.loads(content[0].get("text", "{}"))

                if "error" in episodes_json:
                    print(f"Error from agent: {episodes_json['error']}")
                    return 1

                episodes = episodes_json.get("episodes", [])
                found = episodes_json.get("episodes_found", 0)
                agent = episodes_json.get("agent_id", agent_id)

                print(f"Agent: {agent}")
                print(f"Episodes found: {found}")
                print("=" * 70)

                for i, ep in enumerate(episodes, 1):
                    print(f"\n[{i}] Episode: {ep.get('id', 'N/A')}")
                    print(f"    Agent: {ep.get('agent_id', 'N/A')}")
                    print(f"    Model: {ep.get('model_deployment', 'N/A')}")
                    print(f"    Latency: {ep.get('request_latency_ms', 'N/A')}ms")
                    print(f"    Tool calls: {ep.get('tool_calls_count', 0)}")
                    print(f"    Created: {ep.get('created_at', 'N/A')}")
                    user_input = ep.get("user_input", "")
                    print(f"    Input: {user_input[:100]}{'...' if len(user_input) > 100 else ''}")
                    output = ep.get("assistant_output", "")
                    print(f"    Output: {output[:100]}{'...' if len(output) > 100 else ''}")

                print(f"\n{'=' * 70}")
                print(f"Total: {found} episodes captured for agent '{agent}'")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List captured episodes via MCP API")
    parser.add_argument("--port", type=int, default=8000, help="Port for MCP service (default: 8000)")
    parser.add_argument("--agent-id", type=str, default="mcp-agents", help="Agent ID (default: mcp-agents)")
    parser.add_argument("--limit", type=int, default=10, help="Max episodes to return (default: 10)")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args.port, args.agent_id, args.limit)))
