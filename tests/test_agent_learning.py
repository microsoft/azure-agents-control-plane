#!/usr/bin/env python3
"""
Azure Agents Learning SDK End-to-End Demo Script (External via APIM/AKS)

This script tests the complete in-process reinforcement-learning loop of the
Azure Agents Learning SDK (https://github.com/microsoft/azure-agents-learning-sdk)
via APIM and AKS using the 12 learning MCP tools:

Episode Management:
  1. learning_list_episodes      - List captured episodes
  2. learning_get_episode        - Get episode details

Reward Management:
  3. learning_assign_reward      - Assign a manual reward/label to an episode
  4. learning_list_rewards       - List assigned rewards

Judge Scoring:
  5. learning_score_episode      - Score an episode with the Azure AI Evaluation judges
  6. learning_get_metrics        - List stored judge metric results for an episode

Policy Management:
  7. learning_init_policy        - Create/replace the softmax policy from actions
  8. learning_get_policy         - Get the latest policy snapshot + action probabilities

Training Management:
  9. learning_run_training       - Run one offline REINFORCE learning batch
  10. learning_get_training_status - Get a learning run's status
  11. learning_list_training_runs  - List learning runs

Statistics:
  12. learning_get_stats         - Get comprehensive statistics

Prerequisites:
- MCP server deployed to AKS with capture enabled (AGENT_LEARNING_ENABLE_CAPTURE=true)
- APIM configured with OAuth (or use --direct for LoadBalancer)
- Cosmos DB deployed with the learning containers (when AGENT_LEARNING_STORE_BACKEND=cosmos)

Usage:
    python tests/test_agent_learning.py           # Via APIM with OAuth
    python tests/test_agent_learning.py --direct  # Direct via LoadBalancer
"""

import asyncio
import json
import logging
import os
import sys
import uuid
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import aiohttp

# Configuration file
CONFIG_FILE = Path(__file__).parent / 'mcp_test_config.json'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Demo configuration
DEMO_AGENT_ID = "mcp-agents"

# Sample discrete action space used to initialize a policy for the demo.
DEMO_ACTIONS = ["concise", "detailed"]

# All 12 learning MCP tools
LEARNING_MCP_TOOLS = [
    # Episode Management
    "learning_list_episodes",
    "learning_get_episode",
    # Reward Management
    "learning_assign_reward",
    "learning_list_rewards",
    # Judge Scoring
    "learning_score_episode",
    "learning_get_metrics",
    # Policy Management
    "learning_init_policy",
    "learning_get_policy",
    # Training Management
    "learning_run_training",
    "learning_get_training_status",
    "learning_list_training_runs",
    # Statistics
    "learning_get_stats",
]

LEARNING_CONTAINERS = [
    "learning_episodes",
    "learning_rewards",
    "learning_metrics",
    "learning_policies",
    "learning_runs",
]


def load_config() -> Dict[str, Any]:
    """Load configuration from mcp_test_config.json"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            print(f"✅ Loaded configuration from {CONFIG_FILE}")
            return config
    else:
        print(f"⚠️  Config file not found: {CONFIG_FILE}")
        print("   Run scripts/generate-test-config.ps1 to generate it")
        return {}


class MCPClient:
    """MCP Client that maintains SSE session for testing via APIM or direct"""

    def __init__(self, base_url: str, auth_token: str = None):
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.session = None
        self.sse_response = None
        self.session_message_url = None

    async def __aenter__(self):
        cookie_jar = aiohttp.CookieJar()
        headers = {}
        if self.auth_token and not self.auth_token.startswith('direct-mode'):
            headers['Authorization'] = f'Bearer {self.auth_token}'
        self.session = aiohttp.ClientSession(
            cookie_jar=cookie_jar,
            headers=headers
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.sse_response and not self.sse_response.closed:
            self.sse_response.close()
        if self.session:
            await self.session.close()

    async def establish_sse_session(self) -> bool:
        """Establish SSE connection and extract session URL"""
        try:
            print(f"\n📡 Establishing SSE session to: {self.base_url}/sse")

            self.sse_response = await self.session.get(
                f'{self.base_url}/sse',
                headers={
                    'Accept': 'text/event-stream',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )

            print(f"   SSE Response Status: {self.sse_response.status}")

            if self.sse_response.status == 200:
                async for chunk in self.sse_response.content.iter_chunked(1024):
                    if chunk:
                        data = chunk.decode('utf-8', errors='ignore')
                        match = re.search(r'data: (message\?[^\n\r]+)', data)
                        if match:
                            session_path = match.group(1)
                            self.session_message_url = f"{self.base_url}/{session_path}"
                            print(f"✅ Got session URL: {self.session_message_url}")
                            return True
                        break

                print("⚠️  SSE connected but no session URL found")
                return False
            else:
                response_text = await self.sse_response.text()
                print(f"❌ SSE connection failed: {response_text}")
                return False

        except Exception as e:
            print(f"❌ SSE connection error: {e}")
            return False

    async def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a JSON-RPC 2.0 request"""
        request_id = f"test-{uuid.uuid4().hex[:8]}"

        jsonrpc_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method
        }

        if params:
            jsonrpc_request["params"] = params

        message_url = self.session_message_url if self.session_message_url else f'{self.base_url}/message'

        try:
            async with self.session.post(
                message_url,
                json=jsonrpc_request,
                headers={'Content-Type': 'application/json'},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                response_text = await response.text()

                if response.status == 200:
                    try:
                        return json.loads(response_text)
                    except json.JSONDecodeError:
                        return {"error": "Invalid JSON response", "raw": response_text}
                else:
                    return {"error": f"HTTP {response.status}", "body": response_text}

        except asyncio.TimeoutError:
            return {"error": "Request timed out"}
        except Exception as e:
            return {"error": str(e)}

    async def list_tools(self) -> Optional[List[Dict[str, Any]]]:
        """List available MCP tools"""
        result = await self.send_request("tools/list")
        if 'error' not in result:
            return result.get('result', {}).get('tools', [])
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an MCP tool"""
        return await self.send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })

    async def get_health(self) -> Dict[str, Any]:
        """Get health status from the server"""
        try:
            # Try direct health endpoint (not via MCP protocol)
            health_url = self.base_url.replace('/sse', '').replace('/runtime/webhooks/mcp', '') + '/health'
            async with self.session.get(health_url) as response:
                if response.status == 200:
                    return await response.json()
                return {"error": f"HTTP {response.status}"}
        except Exception as e:
            return {"error": str(e)}


async def get_mcp_token(session: aiohttp.ClientSession, token_url: str) -> Optional[str]:
    """Get MCP access token from APIM OAuth endpoint"""
    print("\n🔐 Getting MCP access token from APIM...")
    print(f"   Token URL: {token_url}")

    try:
        async with session.post(token_url, data={}) as response:
            if response.status == 200:
                data = await response.json()
                token = data.get('access_token', '')
                if token:
                    print(f"✅ Got MCP access token: {token[:30]}...")
                    return token
            print(f"❌ Token request failed: {response.status}")
            return None
    except Exception as e:
        print(f"❌ Error getting token: {e}")
        return None


async def check_cosmos_learning_containers() -> Dict[str, Any]:
    """
    Check if the Learning SDK Cosmos containers exist by querying the account.
    Returns status of each container.
    """
    print("\n🗄️  Checking Azure Agents Learning SDK Cosmos DB containers...")

    cosmos_uri = os.getenv("AGENT_LEARNING_COSMOS_ENDPOINT", "") or os.getenv("COSMOSDB_ENDPOINT", "")
    db_name = os.getenv("AGENT_LEARNING_COSMOS_DATABASE", "agent_learning")

    if not cosmos_uri:
        print("   ⚠️  AGENT_LEARNING_COSMOS_ENDPOINT not set - cannot verify containers directly")
        print("   ℹ️  Containers will be verified via MCP tool calls")
        return {"verified_directly": False, "containers": {}}

    try:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        print(f"   Connecting to Cosmos: {cosmos_uri[:50]}...")
        print(f"   Database: {db_name}")

        credential = DefaultAzureCredential()
        client = CosmosClient(cosmos_uri, credential=credential)

        # Try to get the agent_learning database
        try:
            database = client.get_database_client(db_name)
            # List containers
            containers = list(database.list_containers())
            container_names = [c['id'] for c in containers]

            print(f"\n   Found {len(container_names)} containers in '{db_name}':")

            results = {"verified_directly": True, "database": db_name, "containers": {}}

            for expected_container in LEARNING_CONTAINERS:
                if expected_container in container_names:
                    print(f"   ✅ {expected_container}")
                    results["containers"][expected_container] = "exists"

                    # Count documents
                    try:
                        container = database.get_container_client(expected_container)
                        count_query = "SELECT VALUE COUNT(1) FROM c"
                        count_result = list(container.query_items(count_query, enable_cross_partition_query=True))
                        doc_count = count_result[0] if count_result else 0
                        results["containers"][expected_container] = f"exists ({doc_count} docs)"
                        print(f"      📄 {doc_count} documents")
                    except Exception as e:
                        logger.debug(f"Error counting docs: {e}")
                else:
                    print(f"   ❌ {expected_container} - MISSING")
                    results["containers"][expected_container] = "missing"

            return results

        except Exception as e:
            if "ResourceNotFound" in str(e) or "NotFound" in str(e):
                print(f"   ❌ Database '{db_name}' not found!")
                print(f"   ℹ️  Learning containers need to be provisioned.")
                print(f"   📋 See docs/AGENTS_AGENT_LEARNING_DESIGN.md for setup instructions.")
                return {"verified_directly": True, "database": db_name, "error": "database_not_found"}
            raise

    except ImportError:
        print("   ⚠️  azure-cosmos not installed - skipping direct verification")
        return {"verified_directly": False, "containers": {}}
    except Exception as e:
        print(f"   ❌ Error checking containers: {e}")
        return {"verified_directly": False, "error": str(e)}


async def test_mcp_health(client: MCPClient) -> bool:
    """Test MCP server health endpoint"""
    print("\n🏥 Checking MCP server health...")

    health = await client.get_health()

    if 'error' in health:
        print(f"   ⚠️  Could not reach health endpoint: {health['error']}")
        return True  # Not a fatal error, continue with tests

    print(f"   Status: {health.get('status', 'unknown')}")
    print(f"   Timestamp: {health.get('timestamp', 'N/A')}")
    return True


async def test_learning_tools_available(client: MCPClient) -> Dict[str, bool]:
    """Check if all 12 learning MCP tools are available"""
    print("\n🔧 Checking for learning MCP tools (12 expected)...")

    tools = await client.list_tools()

    if not tools:
        print("   ❌ Could not list tools")
        return {}

    print(f"   Found {len(tools)} total tools")

    # Check for all 12 learning tools
    learning_tools_status = {tool: False for tool in LEARNING_MCP_TOOLS}

    tool_names = [t.get('name', '') for t in tools]

    print("\n   Learning MCP Tools:")
    for tool_name in LEARNING_MCP_TOOLS:
        if tool_name in tool_names:
            learning_tools_status[tool_name] = True
            print(f"   ✅ {tool_name}")
        else:
            print(f"   ❌ {tool_name} - MISSING")

    # Count available
    available_count = sum(1 for v in learning_tools_status.values() if v)
    print(f"\n   📊 Learning tools available: {available_count}/{len(LEARNING_MCP_TOOLS)}")

    # Also check core tools
    print("\n   Core MCP Tools:")
    core_tools = ["ask_foundry", "next_best_action", "store_memory", "recall_memory"]
    for tool in core_tools:
        if tool in tool_names:
            print(f"   ✅ {tool}")
        else:
            print(f"   ❌ {tool} - MISSING")

    return learning_tools_status


async def create_episode_via_mcp(client: MCPClient, question: str) -> Dict[str, Any]:
    """
    Create an episode by calling ask_foundry tool.
    When AGENT_LEARNING_ENABLE_CAPTURE=true, this automatically stores an episode.
    """
    print(f"\n📝 Creating episode via ask_foundry...")
    print(f"   Question: {question[:60]}...")

    result = await client.call_tool("ask_foundry", {"question": question})

    if 'error' in result:
        print(f"   ❌ Error: {result['error']}")
        return {"success": False, "error": result['error']}

    tool_result = result.get('result', {})
    content = tool_result.get('content', [])
    is_error = tool_result.get('isError', False)

    if is_error:
        error_text = content[0].get('text', 'Unknown error') if content else 'No error message'
        print(f"   ❌ Tool error: {error_text[:100]}...")
        return {"success": False, "error": error_text}

    response_text = content[0].get('text', '') if content else ''
    print(f"   ✅ Response: {response_text[:100]}...")

    return {
        "success": True,
        "question": question,
        "response": response_text,
        "timestamp": datetime.utcnow().isoformat(),
    }


async def test_episode_storage(client: MCPClient) -> List[Dict[str, Any]]:
    """
    Test episode storage by creating multiple episodes via MCP tools.
    Note: Episodes are only stored when AGENT_LEARNING_ENABLE_CAPTURE=true on the server.
    """
    print("\n" + "=" * 60)
    print("📊 Testing Episode Creation via MCP")
    print("=" * 60)

    test_questions = [
        "What is the capital of France?",
        "Calculate 2+2",
        "Explain what machine learning is in one sentence.",
        "What programming language is Python named after?",
        "What is REST API?",
    ]

    episodes = []
    for i, question in enumerate(test_questions, 1):
        print(f"\n--- Episode {i}/{len(test_questions)} ---")
        result = await create_episode_via_mcp(client, question)
        if result.get('success'):
            episodes.append(result)

        # Brief pause between calls
        await asyncio.sleep(1)

    print(f"\n✅ Created {len(episodes)}/{len(test_questions)} episodes")
    return episodes


# =========================================
# Learning MCP Tools Tests
# =========================================

async def call_learning_tool(client: MCPClient, tool_name: str, arguments: Dict[str, Any] = None) -> Dict[str, Any]:
    """Helper to call a learning MCP tool and parse the response"""
    result = await client.call_tool(tool_name, arguments or {})

    if 'error' in result:
        return {"success": False, "error": result['error']}

    tool_result = result.get('result', {})
    content = tool_result.get('content', [])
    is_error = tool_result.get('isError', False)

    if is_error:
        error_text = content[0].get('text', 'Unknown error') if content else 'No error message'
        return {"success": False, "error": error_text}

    try:
        response_text = content[0].get('text', '{}') if content else '{}'
        response_data = json.loads(response_text)
        return {"success": True, "data": response_data}
    except json.JSONDecodeError:
        return {"success": True, "raw": response_text}


async def test_learning_get_stats(client: MCPClient) -> Dict[str, Any]:
    """Test learning_get_stats tool"""
    print("\n" + "=" * 60)
    print("📊 Testing learning_get_stats")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_get_stats", {"agent_id": DEMO_AGENT_ID})

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    stats = data.get('statistics', {})
    policy = data.get('active_policy', {})

    print(f"\n   Agent ID: {data.get('agent_id', 'N/A')}")
    print(f"   Capture Enabled: {data.get('capture_enabled', False)}")
    print(f"   Model Deployment: {data.get('model_deployment', 'N/A')}")
    print(f"\n   Statistics:")
    print(f"     Total Episodes: {stats.get('total_episodes', 0)}")
    print(f"     Total Rewards: {stats.get('total_rewards', 0)}")
    print(f"     Average Aggregate Reward: {stats.get('average_aggregate_reward', 0)}")
    print(f"     Total Training Runs: {stats.get('total_training_runs', 0)}")

    if policy.get('has_policy'):
        print(f"\n   Active Policy: v{policy.get('version', 0)} ({len(policy.get('actions', []))} actions)")
        print(f"     Updates Applied: {policy.get('updates_applied', 0)}")
    else:
        print(f"\n   Active Policy: None (initialize with learning_init_policy)")

    return result


async def test_learning_list_episodes(client: MCPClient) -> Dict[str, Any]:
    """Test learning_list_episodes tool"""
    print("\n" + "=" * 60)
    print("📋 Testing learning_list_episodes")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_list_episodes", {
        "agent_id": DEMO_AGENT_ID,
        "limit": 10
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    episodes = data.get('episodes', [])

    print(f"\n   Found {data.get('episodes_found', 0)} episodes")

    for i, ep in enumerate(episodes[:5], 1):
        print(f"\n   Episode {i}:")
        print(f"     ID: {ep.get('id', 'N/A')[:20]}...")
        print(f"     Input: {ep.get('user_input', 'N/A')[:50]}...")
        print(f"     Action: {ep.get('action_id', 'N/A')}")
        print(f"     Latency: {ep.get('request_latency_ms', 'N/A')}ms")

    if len(episodes) > 5:
        print(f"\n   ... and {len(episodes) - 5} more episodes")

    return result


async def test_learning_get_episode(client: MCPClient, episode_id: str) -> Dict[str, Any]:
    """Test learning_get_episode tool"""
    print(f"\n📖 Testing learning_get_episode (ID: {episode_id[:20]}...)")

    result = await call_learning_tool(client, "learning_get_episode", {
        "episode_id": episode_id,
        "agent_id": DEMO_AGENT_ID
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    print(f"   ✅ Retrieved episode details")
    print(f"     User Input: {data.get('user_input', 'N/A')[:60]}...")
    print(f"     Assistant Output: {data.get('assistant_output', 'N/A')[:60]}...")
    print(f"     Tool Calls: {len(data.get('tool_calls', []))}")

    return result


async def test_learning_assign_reward(client: MCPClient, episode_id: str, reward_value: float) -> Dict[str, Any]:
    """Test learning_assign_reward tool"""
    print(f"\n🏅 Testing learning_assign_reward (Episode: {episode_id[:20]}..., Value: {reward_value})")

    result = await call_learning_tool(client, "learning_assign_reward", {
        "episode_id": episode_id,
        "reward_value": reward_value,
        "reward_source": "human_approval",
        "agent_id": DEMO_AGENT_ID,
        "evaluator": "test_script",
        "comments": f"Test reward assigned at {datetime.utcnow().isoformat()}"
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    print(f"   ✅ Reward assigned: {data.get('reward_id', 'N/A')[:20]}...")
    print(f"     Value: {data.get('value', 'N/A')}")
    print(f"     Source: {data.get('source', 'N/A')}")

    return result


async def test_learning_list_rewards(client: MCPClient, episode_id: str = None) -> Dict[str, Any]:
    """Test learning_list_rewards tool"""
    print("\n" + "=" * 60)
    print("🏆 Testing learning_list_rewards")
    print("=" * 60)

    args = {"agent_id": DEMO_AGENT_ID, "limit": 20}
    if episode_id:
        args["episode_id"] = episode_id

    result = await call_learning_tool(client, "learning_list_rewards", args)

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    rewards = data.get('rewards', [])

    print(f"\n   Found {data.get('rewards_found', 0)} rewards")

    for i, r in enumerate(rewards[:5], 1):
        print(f"\n   Reward {i}:")
        print(f"     ID: {r.get('id', 'N/A')[:20]}...")
        print(f"     Episode: {r.get('episode_id', 'N/A')[:20]}...")
        print(f"     Value: {r.get('value', 'N/A')}")
        print(f"     Source: {r.get('source', 'N/A')}")

    return result


async def test_learning_score_episode(client: MCPClient, episode_id: str) -> Dict[str, Any]:
    """Test learning_score_episode tool (runs the Azure AI Evaluation judges)"""
    print("\n" + "=" * 60)
    print(f"⚖️  Testing learning_score_episode (Episode: {episode_id[:20]}...)")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_score_episode", {
        "episode_id": episode_id,
        "agent_id": DEMO_AGENT_ID,
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    print(f"\n   ✅ Episode scored")
    print(f"     Rewards Written: {data.get('rewards_written', 0)}")
    print(f"     Aggregate Reward: {data.get('aggregate_reward', 'N/A')}")
    for r in data.get('rewards', [])[:5]:
        print(f"       • {r.get('source', 'N/A')} ({r.get('metric') or '-'}): {r.get('value')}")

    return result


async def test_learning_get_metrics(client: MCPClient, episode_id: str) -> Dict[str, Any]:
    """Test learning_get_metrics tool"""
    print("\n" + "=" * 60)
    print("📈 Testing learning_get_metrics")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_get_metrics", {
        "episode_id": episode_id,
        "agent_id": DEMO_AGENT_ID,
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    metrics = data.get('metrics', [])

    print(f"\n   Found {data.get('metrics_found', 0)} metric results")

    for m in metrics[:5]:
        print(f"\n   Metric: {m.get('metric', 'N/A')}")
        print(f"     Score: {m.get('score', 'N/A')}  Normalized: {m.get('normalized', 'N/A')}")
        print(f"     Status: {m.get('status', 'N/A')}")

    return result


async def test_learning_init_policy(client: MCPClient) -> Dict[str, Any]:
    """Test learning_init_policy tool"""
    print("\n" + "=" * 60)
    print(f"🧭 Testing learning_init_policy (Actions: {DEMO_ACTIONS})")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_init_policy", {
        "actions": DEMO_ACTIONS,
        "agent_id": DEMO_AGENT_ID,
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    print(f"\n   ✅ Policy initialized")
    print(f"     Policy ID: {data.get('policy_id', 'N/A')[:20]}...")
    print(f"     Version: {data.get('version', 'N/A')}")
    print(f"     Actions: {data.get('actions', [])}")

    return result


async def test_learning_get_policy(client: MCPClient) -> Dict[str, Any]:
    """Test learning_get_policy tool"""
    print("\n" + "=" * 60)
    print("🧠 Testing learning_get_policy")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_get_policy", {
        "agent_id": DEMO_AGENT_ID
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})

    if data.get('has_policy'):
        print(f"\n   ✅ Active policy found:")
        print(f"     Policy ID: {data.get('policy_id', 'N/A')[:20]}...")
        print(f"     Version: {data.get('version', 'N/A')}")
        print(f"     Action Probabilities: {data.get('action_probabilities', {})}")
        print(f"     Updates Applied: {data.get('updates_applied', 0)}")
    else:
        print(f"\n   ℹ️  No policy yet - initialize with learning_init_policy")

    return result


async def test_learning_run_training(client: MCPClient) -> Dict[str, Any]:
    """Test learning_run_training tool (one offline REINFORCE batch)"""
    print("\n" + "=" * 60)
    print("🏋️  Testing learning_run_training")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_run_training", {
        "agent_id": DEMO_AGENT_ID,
        "limit": 100,
        "score_missing": True,
    })

    if not result.get('success'):
        error = result.get('error', 'Unknown')
        if "No policy found" in error:
            print(f"   ⚠️  No policy yet - run learning_init_policy first")
        else:
            print(f"   ❌ Error: {error[:100]}")
        return result

    data = result.get('data', {})
    print(f"\n   ✅ Training run completed")
    print(f"     Run ID: {data.get('training_run_id', 'N/A')[:20]}...")
    print(f"     Algorithm: {data.get('algorithm', 'N/A')}")
    print(f"     Status: {data.get('status', 'N/A')}")
    print(f"     Episodes Used: {data.get('episodes_used', 0)}")
    print(f"     Metrics: {data.get('metrics', {})}")

    return result


async def test_learning_get_training_status(client: MCPClient, training_run_id: str) -> Dict[str, Any]:
    """Test learning_get_training_status tool"""
    print(f"\n🔎 Testing learning_get_training_status (Run: {training_run_id[:20]}...)")

    result = await call_learning_tool(client, "learning_get_training_status", {
        "training_run_id": training_run_id,
        "agent_id": DEMO_AGENT_ID,
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    print(f"   ✅ Status: {data.get('status', 'N/A')}")
    print(f"     Episodes Used: {data.get('episodes_used', 0)}")
    print(f"     Metrics: {data.get('metrics', {})}")

    return result


async def test_learning_list_training_runs(client: MCPClient) -> Dict[str, Any]:
    """Test learning_list_training_runs tool"""
    print("\n" + "=" * 60)
    print("🚂 Testing learning_list_training_runs")
    print("=" * 60)

    result = await call_learning_tool(client, "learning_list_training_runs", {
        "agent_id": DEMO_AGENT_ID,
        "limit": 10
    })

    if not result.get('success'):
        print(f"   ❌ Error: {result.get('error', 'Unknown')[:100]}")
        return result

    data = result.get('data', {})
    runs = data.get('training_runs', [])

    print(f"\n   Found {data.get('runs_found', 0)} training runs")

    for i, run in enumerate(runs[:5], 1):
        print(f"\n   Training Run {i}:")
        print(f"     ID: {run.get('id', 'N/A')[:20]}...")
        print(f"     Status: {run.get('status', 'N/A')}")
        print(f"     Algorithm: {run.get('algorithm', 'N/A')}")
        print(f"     Episodes Used: {run.get('episodes_used', 0)}")

    return result


async def run_full_learning_loop_test(client: MCPClient) -> Dict[str, Any]:
    """
    Run the complete learning MCP tools test loop.
    Tests all 12 learning tools in the correct workflow order.
    """
    print("\n" + "=" * 70)
    print("🎓 AZURE AGENTS LEARNING SDK FULL LOOP TEST (Using 12 MCP Tools)")
    print("=" * 70)

    results = {
        "stats": None,
        "episodes_listed": None,
        "episode_details": None,
        "reward_assigned": None,
        "rewards_listed": None,
        "episode_scored": None,
        "metrics_listed": None,
        "policy_initialized": None,
        "policy_snapshot": None,
        "training_run": None,
        "training_status": None,
        "training_runs_listed": None,
    }

    # Step 1: Get overall learning stats
    print("\n\n📊 STEP 1: Get Learning Statistics")
    results["stats"] = await test_learning_get_stats(client)
    await asyncio.sleep(1)

    # Step 2: List existing episodes
    print("\n\n📋 STEP 2: List Episodes")
    results["episodes_listed"] = await test_learning_list_episodes(client)
    await asyncio.sleep(1)

    # Step 3-7: Episode-scoped operations (if any episodes exist)
    episodes_data = results["episodes_listed"].get('data', {}).get('episodes', [])
    first_episode_id = episodes_data[0].get('id') if episodes_data else None

    if first_episode_id:
        print("\n\n📖 STEP 3: Get Episode Details")
        results["episode_details"] = await test_learning_get_episode(client, first_episode_id)
        await asyncio.sleep(1)

        print("\n\n🏅 STEP 4: Assign Reward to Episode")
        results["reward_assigned"] = await test_learning_assign_reward(client, first_episode_id, 0.8)
        await asyncio.sleep(1)

        print("\n\n⚖️  STEP 6: Score Episode with Judges")
        results["episode_scored"] = await test_learning_score_episode(client, first_episode_id)
        await asyncio.sleep(1)

        print("\n\n📈 STEP 7: Get Judge Metrics")
        results["metrics_listed"] = await test_learning_get_metrics(client, first_episode_id)
        await asyncio.sleep(1)
    else:
        print("\n\n⚠️  STEP 3-7: Skipped (no episodes found)")
        print("   Run ask_foundry with AGENT_LEARNING_ENABLE_CAPTURE=true to create episodes")

    # Step 5: List rewards
    print("\n\n🏆 STEP 5: List Rewards")
    results["rewards_listed"] = await test_learning_list_rewards(client)
    await asyncio.sleep(1)

    # Step 8: Initialize the policy
    print("\n\n🧭 STEP 8: Initialize Policy")
    results["policy_initialized"] = await test_learning_init_policy(client)
    await asyncio.sleep(1)

    # Step 9: Inspect the policy
    print("\n\n🧠 STEP 9: Get Policy Snapshot")
    results["policy_snapshot"] = await test_learning_get_policy(client)
    await asyncio.sleep(1)

    # Step 10: Run one offline learning batch
    print("\n\n🏋️  STEP 10: Run Training Batch")
    results["training_run"] = await test_learning_run_training(client)
    await asyncio.sleep(1)

    # Step 11: Get the status of the run we just created
    run_id = results["training_run"].get('data', {}).get('training_run_id') if results["training_run"] else None
    if run_id:
        print("\n\n🔎 STEP 11: Get Training Status")
        results["training_status"] = await test_learning_get_training_status(client, run_id)
        await asyncio.sleep(1)

    # Step 12: List all training runs
    print("\n\n🚂 STEP 12: List Training Runs")
    results["training_runs_listed"] = await test_learning_list_training_runs(client)

    return results


async def test_next_best_action(client: MCPClient) -> Dict[str, Any]:
    """
    Test the next_best_action tool which uses multiple memory layers.
    This exercises the Cosmos-backed memory systems.
    """
    print("\n" + "=" * 60)
    print("🎯 Testing next_best_action Tool")
    print("=" * 60)

    test_task = "Analyze customer data to identify customers at high risk of churning and create a retention strategy"

    print(f"\n📝 Task: {test_task}")
    print("\n⏳ Processing task (this may take 30-60 seconds)...")
    print("   • Generating embeddings")
    print("   • Searching short-term memory (CosmosDB)")
    print("   • Searching long-term memory (AI Search)")
    print("   • Querying facts memory (Fabric IQ)")
    print("   • Generating action plan")

    result = await client.call_tool("next_best_action", {"task": test_task})

    if 'error' in result:
        print(f"\n❌ Error: {result['error']}")
        return {"success": False, "error": result['error']}

    tool_result = result.get('result', {})
    content = tool_result.get('content', [])
    is_error = tool_result.get('isError', False)

    if is_error:
        error_text = content[0].get('text', 'Unknown error') if content else 'No error message'
        print(f"\n❌ Tool error: {error_text}")

        # Check for specific errors
        if 'CosmosDB not configured' in error_text:
            print("\n⚠️  CosmosDB is not configured on the MCP server.")
            print("   Verify COSMOSDB_ENDPOINT is set in the Kubernetes deployment.")
        elif 'Foundry endpoint not configured' in error_text:
            print("\n⚠️  Foundry endpoint is not configured.")
            print("   Verify FOUNDRY_PROJECT_ENDPOINT is set.")

        return {"success": False, "error": error_text}

    # Parse successful response
    try:
        response_text = content[0].get('text', '{}') if content else '{}'
        response_data = json.loads(response_text)

        print(f"\n✅ Task processed successfully!")
        print(f"   Task ID: {response_data.get('task_id', 'N/A')}")
        print(f"   Intent: {response_data.get('intent', 'N/A')}")

        analysis = response_data.get('analysis', {})
        print(f"   Similar tasks found: {analysis.get('similar_tasks_found', 0)}")
        print(f"   Task instructions found: {analysis.get('task_instructions_found', 0)}")
        print(f"   Domain facts found: {analysis.get('domain_facts_found', 0)}")

        plan = response_data.get('plan', {})
        print(f"   Plan steps: {plan.get('total_steps', 0)}")

        metadata = response_data.get('metadata', {})
        print(f"   Stored in Cosmos: {metadata.get('stored_in_cosmos', False)}")

        return {"success": True, "data": response_data}

    except json.JSONDecodeError as e:
        print(f"\n⚠️  Could not parse response: {e}")
        return {"success": True, "raw_response": response_text}


async def test_memory_operations(client: MCPClient, session_id: str) -> Dict[str, Any]:
    """
    Test short-term memory operations (store and recall).
    """
    print("\n" + "=" * 60)
    print("💾 Testing Memory Operations")
    print("=" * 60)

    results = {"store": [], "recall": []}

    # Store some test memories
    test_memories = [
        {"content": "The customer prefers email communication.", "type": "context"},
        {"content": "Previous meeting was about Q4 strategy.", "type": "conversation"},
        {"content": "TODO: Follow up on the proposal by Friday.", "type": "task"},
    ]

    print(f"\n📥 Storing {len(test_memories)} memories...")

    for mem in test_memories:
        result = await client.call_tool("store_memory", {
            "content": mem["content"],
            "session_id": session_id,
            "memory_type": mem["type"],
        })

        tool_result = result.get('result', {})
        content = tool_result.get('content', [])

        if content and not tool_result.get('isError'):
            try:
                response = json.loads(content[0].get('text', '{}'))
                if response.get('success'):
                    print(f"   ✅ Stored: {mem['content'][:40]}...")
                    results["store"].append({"success": True, "memory_id": response.get('memory_id')})
                else:
                    print(f"   ❌ Failed: {response.get('error', 'Unknown')}")
                    results["store"].append({"success": False, "error": response.get('error')})
            except json.JSONDecodeError:
                print(f"   ⚠️  Invalid response")
                results["store"].append({"success": False, "error": "Invalid JSON"})
        else:
            error = content[0].get('text', 'Unknown error') if content else 'No response'
            print(f"   ❌ Error: {error[:50]}...")
            results["store"].append({"success": False, "error": error})

    # Recall memories
    print(f"\n📤 Recalling memories...")

    recall_queries = [
        "customer communication preferences",
        "meeting notes and strategy",
    ]

    for query in recall_queries:
        result = await client.call_tool("recall_memory", {
            "query": query,
            "session_id": session_id,
            "limit": 3,
        })

        tool_result = result.get('result', {})
        content = tool_result.get('content', [])

        if content and not tool_result.get('isError'):
            try:
                response = json.loads(content[0].get('text', '{}'))
                memories_found = response.get('memories_found', 0)
                print(f"   ✅ Query '{query[:30]}...' found {memories_found} memories")
                results["recall"].append({"success": True, "query": query, "found": memories_found})
            except json.JSONDecodeError:
                print(f"   ⚠️  Invalid response")
                results["recall"].append({"success": False, "query": query, "error": "Invalid JSON"})
        else:
            error = content[0].get('text', 'Unknown error') if content else 'No response'
            print(f"   ❌ Query '{query[:30]}...' failed: {error[:30]}...")
            results["recall"].append({"success": False, "query": query, "error": error})

    return results


def print_learning_summary(
    tools_available: Dict[str, bool],
    learning_results: Dict[str, Any],
    episodes_created: int = 0,
):
    """Print a summary of learning MCP tools test results"""
    print("\n" + "=" * 70)
    print("📋 AZURE AGENTS LEARNING SDK TEST SUMMARY")
    print("=" * 70)

    # Learning MCP Tools availability
    available_count = sum(1 for v in tools_available.values() if v)
    total_tools = len(LEARNING_MCP_TOOLS)
    print(f"\n🎓 Learning MCP Tools: {available_count}/{total_tools} available")

    if available_count < total_tools:
        missing = [k for k, v in tools_available.items() if not v]
        print(f"   ❌ Missing: {', '.join(missing[:5])}")
        if len(missing) > 5:
            print(f"      ... and {len(missing) - 5} more")
    else:
        print("   ✅ All 12 learning MCP tools available!")

    # Episodes
    if episodes_created > 0:
        print(f"\n📝 Episodes Created: {episodes_created}")
        print("   ℹ️  Episodes stored when AGENT_LEARNING_ENABLE_CAPTURE=true")

    # Learning Stats
    stats_result = learning_results.get("stats", {})
    if stats_result.get("success"):
        data = stats_result.get("data", {})
        stats = data.get("statistics", {})
        policy = data.get("active_policy", {})
        print(f"\n📊 Learning Statistics:")
        print(f"   • Episodes: {stats.get('total_episodes', 0)}")
        print(f"   • Rewards: {stats.get('total_rewards', 0)}")
        print(f"   • Training Runs: {stats.get('total_training_runs', 0)}")

        if policy.get("has_policy"):
            print(f"\n   🧠 Active Policy: v{policy.get('version', 0)} ({len(policy.get('actions', []))} actions)")
        else:
            print(f"\n   📌 No policy yet (base model: {data.get('model_deployment', 'N/A')})")
    else:
        print(f"\n📊 Learning Statistics: ❌ Could not retrieve")

    # Test Results Summary
    print(f"\n🧪 Test Results:")

    test_checks = [
        ("Get Stats", learning_results.get("stats", {}).get("success", False)),
        ("List Episodes", learning_results.get("episodes_listed", {}).get("success", False)),
        ("Get Episode", learning_results.get("episode_details", {}).get("success", False) if learning_results.get("episode_details") else None),
        ("Assign Reward", learning_results.get("reward_assigned", {}).get("success", False) if learning_results.get("reward_assigned") else None),
        ("List Rewards", learning_results.get("rewards_listed", {}).get("success", False)),
        ("Score Episode", learning_results.get("episode_scored", {}).get("success", False) if learning_results.get("episode_scored") else None),
        ("Get Metrics", learning_results.get("metrics_listed", {}).get("success", False) if learning_results.get("metrics_listed") else None),
        ("Init Policy", learning_results.get("policy_initialized", {}).get("success", False)),
        ("Get Policy", learning_results.get("policy_snapshot", {}).get("success", False)),
        ("Run Training", learning_results.get("training_run", {}).get("success", False)),
        ("Get Training Status", learning_results.get("training_status", {}).get("success", False) if learning_results.get("training_status") else None),
        ("List Training Runs", learning_results.get("training_runs_listed", {}).get("success", False)),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, result in test_checks:
        if result is None:
            print(f"   ⏭️  {name}: Skipped")
            skipped += 1
        elif result:
            print(f"   ✅ {name}: Passed")
            passed += 1
        else:
            print(f"   ❌ {name}: Failed")
            failed += 1

    # Overall result
    print("\n" + "=" * 70)

    if available_count == total_tools and failed == 0:
        print("🎉 ALL LEARNING TESTS PASSED!")
    elif available_count == 0:
        print("❌ Learning tools not available - check server deployment")
    else:
        print(f"⚠️  Results: {passed} passed, {failed} failed, {skipped} skipped")

    print("=" * 70)

    # Next steps
    print("\n📋 Next Steps for the Full Learning Loop:")

    stats_data = stats_result.get("data", {}).get("statistics", {}) if stats_result.get("success") else {}

    if stats_data.get("total_episodes", 0) == 0:
        print("\n   1. Create Episodes:")
        print("      • Call ask_foundry or next_best_action tools")
        print("      • Ensure AGENT_LEARNING_ENABLE_CAPTURE=true on server")

    if stats_data.get("total_rewards", 0) == 0:
        print("\n   2. Produce Rewards:")
        print("      • Use learning_score_episode to run the judges, or")
        print("      • Use learning_assign_reward to label episodes manually")

    policy_data = stats_result.get("data", {}).get("active_policy", {}) if stats_result.get("success") else {}
    if not policy_data.get("has_policy"):
        print("\n   3. Initialize a Policy:")
        print("      • Use learning_init_policy with your discrete action choices")

    if stats_data.get("total_training_runs", 0) == 0:
        print("\n   4. Run Training:")
        print("      • Use learning_run_training to apply one REINFORCE update")
        print("      • Inspect the result with learning_get_policy")

    print("\n   📚 See docs/AGENTS_AGENT_LEARNING_DESIGN.md for the complete guide")


async def main():
    print("=" * 70)
    print("🎓 Azure Agents Learning SDK End-to-End Test (Using 12 MCP Tools)")
    print("=" * 70)

    # Check for --direct mode
    use_direct = '--direct' in sys.argv
    skip_episode_creation = '--skip-episodes' in sys.argv

    # Load configuration
    config = load_config()

    # Determine connection mode
    if use_direct:
        direct_config = config.get('direct', {})
        base_url = direct_config.get('base_url', 'http://localhost:8000/runtime/webhooks/mcp')
        token = 'direct-mode-no-token-needed'
        print(f"\n🔗 Using Direct Mode: {base_url}")
        print("   (Via LoadBalancer or port-forward)")
    else:
        apim_config = config.get('apim', {})
        base_url = apim_config.get('base_url', '')
        token_url = apim_config.get('oauth_token_url', '')

        if not base_url:
            print("\n❌ No APIM base URL configured")
            print("   Run: scripts/generate-test-config.ps1")
            print("   Or use: python test_agent_learning.py --direct")
            return 1

        print(f"\n🔗 Using APIM: {base_url}")

        # Get OAuth token
        async with aiohttp.ClientSession() as session:
            token = await get_mcp_token(session, token_url)
            if not token:
                print("❌ Failed to get access token")
                print("   Try using --direct mode instead")
                return 1

    # Run tests via MCP
    async with MCPClient(base_url, token) as client:
        # Establish SSE session
        if not await client.establish_sse_session():
            print("\n❌ Failed to establish SSE session")
            print("   Check that the MCP server is running and accessible")
            return 1

        # Wait for session initialization
        print("\n⏳ Waiting for session to initialize...")
        await asyncio.sleep(2)

        # Test health
        await test_mcp_health(client)

        # Check all 12 learning MCP tools
        tools_available = await test_learning_tools_available(client)

        if not tools_available:
            print("\n❌ Could not verify tools - aborting tests")
            return 1

        # Check if learning tools are available
        learning_tools_count = sum(1 for t, v in tools_available.items() if v)

        if learning_tools_count == 0:
            print("\n❌ No learning MCP tools found!")
            print("   Ensure the MCP server includes the learning tools.")
            return 1

        episodes_created = 0

        # Optionally create episodes first
        if not skip_episode_creation:
            print("\n" + "=" * 70)
            print("📝 PHASE 1: Creating Episodes via ask_foundry")
            print("=" * 70)
            episodes = await test_episode_storage(client)
            episodes_created = len(episodes)

            # Brief pause to allow episode capture
            print("\n⏳ Waiting for episode capture to complete...")
            await asyncio.sleep(3)
        else:
            print("\n⏭️  Skipping episode creation (--skip-episodes)")

        # Run the full learning MCP tools test loop
        print("\n" + "=" * 70)
        print("🎓 PHASE 2: Testing Learning MCP Tools")
        print("=" * 70)
        learning_results = await run_full_learning_loop_test(client)

        # Print comprehensive summary
        print_learning_summary(
            tools_available=tools_available,
            learning_results=learning_results,
            episodes_created=episodes_created,
        )

    return 0


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(result)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
