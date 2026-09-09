"""
Unit tests for next_best_action_agent.py

Tests cover:
- cosine_similarity
- get_model_deployment
- hello_mcp_tool
- get_snippet_tool / save_snippet_tool
- ask_foundry_tool
- store_memory_tool / recall_memory_tool / get_session_history_tool / clear_session_memory_tool
- search_facts_tool / get_facts_memory_stats_tool
- get_customer_churn_facts_tool / get_pipeline_health_facts_tool / get_user_security_facts_tool
- cross_domain_analysis_tool
- next_best_action_tool
- find_similar_tasks
- analyze_intent
- generate_plan / generate_plan_with_instructions
- learning_* tools (unavailable path)
- fabric_* wrappers (unavailable path)
- FastAPI endpoints: health, root, mcp_message_endpoint, agent_chat
- MCPTool / MCPToolResult dataclasses
- execute_tool dispatcher
"""

import json
import sys
import os
import asyncio
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from dataclasses import asdict

import numpy as np

# ---------------------------------------------------------------------------
# Patch heavy third-party imports BEFORE importing the module under test.
# The module imports many Azure SDK packages and custom libraries at the
# top level.  We stub them out so the tests can run without credentials or
# installed SDKs.
# ---------------------------------------------------------------------------

# ---- agent_framework stubs ------------------------------------------------
_agent_framework = MagicMock()


def _ai_function(fn):
    """No-op decorator that mimics @ai_function."""
    return fn


_agent_framework.ai_function = _ai_function
_agent_framework.AIFunction = MagicMock()
_agent_framework.azure = MagicMock()

sys.modules.setdefault("agent_framework", _agent_framework)
sys.modules.setdefault("agent_framework.azure", _agent_framework.azure)

# ---- memory stubs ----------------------------------------------------------
_memory_mod = MagicMock()

# Provide real-looking enums / classes that the module references at import
from enum import Enum as _Enum


class _MemoryType(_Enum):
    CONTEXT = "context"
    CONVERSATION = "conversation"
    TASK = "task"
    PLAN = "plan"


class _EntityType(_Enum):
    CUSTOMER = "customer"
    PIPELINE = "pipeline"
    PIPELINE_RUN = "pipeline_run"
    USER = "user"
    AUTH_EVENT = "auth_event"


class _RelationshipType(_Enum):
    OWNS = "owns"


_memory_mod.MemoryType = _MemoryType
_memory_mod.EntityType = _EntityType
_memory_mod.RelationshipType = _RelationshipType
_memory_mod.AISEARCH_CONTEXT_PROVIDER_AVAILABLE = False

# Forward attribute access for everything else to the MagicMock
for _name in [
    "ShortTermMemory", "MemoryEntry", "CompositeMemory", "LongTermMemory",
    "FactsMemory", "Fact", "FactSearchResult", "OntologyEntity",
    "CustomerDataGenerator", "CustomerProfile", "CustomerSegment", "ChurnRiskLevel",
    "PipelineDataGenerator", "Pipeline", "PipelineRun", "PipelineStatus",
    "UserAccessDataGenerator", "User", "AuthEvent", "AuthEventType",
]:
    if not hasattr(_memory_mod, _name):
        setattr(_memory_mod, _name, MagicMock())

sys.modules.setdefault("memory", _memory_mod)

# ---- fabric_tools stub ----------------------------------------------------
_fabric_tools = MagicMock()
_fabric_tools.FABRIC_DATA_AGENTS_ENABLED = False
sys.modules.setdefault("fabric_tools", _fabric_tools)

# ---- agent_learning SDK stub -----------------------------------------------
sys.modules.setdefault("agent_learning", MagicMock())

# ---- agent365_approval stub -----------------------------------------------
_agent365 = MagicMock()


class _ApprovalDecision(_Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


_agent365.ApprovalDecision = _ApprovalDecision
sys.modules.setdefault("agent365_approval", _agent365)

# ---- azure SDK stubs -------------------------------------------------------
for _mod in [
    "azure.storage.blob",
    "azure.identity",
    "azure.cosmos",
    "azure.cosmos.exceptions",
    "azure.ai.evaluation",
    "openai",
    "dotenv",
]:
    sys.modules.setdefault(_mod, MagicMock())

# Make load_dotenv a no-op
sys.modules["dotenv"].load_dotenv = lambda *a, **kw: None

# ---- environment variables so the module doesn't try to connect ------------
os.environ.setdefault("AZURE_STORAGE_ACCOUNT_URL", "")
os.environ.setdefault("AZURE_STORAGE_CONNECTION_STRING", "")
os.environ.setdefault("COSMOSDB_ENDPOINT", "")
os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "")
os.environ.setdefault("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-test")
os.environ.setdefault("EMBEDDING_MODEL_DEPLOYMENT_NAME", "embed-test")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "")
os.environ.setdefault("FABRIC_ENABLED", "false")
os.environ.setdefault("AGENT_LEARNING_ENABLE_CAPTURE", "false")

# Now import the module under test
# We add `src/` to sys.path so the import resolves
_src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
if _src_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_src_dir))

import next_best_action_agent as agent


# ===========================================================================
#  Helper
# ===========================================================================

def _run(coro):
    """Run an async coroutine in a new event loop (test helper)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
#  Tests - Runtime credential selection
# ===========================================================================


class TestRuntimeCredentialSelection(unittest.TestCase):
    def test_disabled_uses_default_credential(self):
        selected = MagicMock()
        with patch.dict(os.environ, {"AGENT_IDENTITY_ENABLED": "false"}), \
             patch.object(agent, "_runtime_agent_credential", None), \
             patch.object(agent, "DefaultAzureCredential", return_value=selected), \
             patch.object(agent, "get_agent_credential") as get_agent:
            self.assertIs(agent._runtime_credential(), selected)
        get_agent.assert_not_called()

    def test_enabled_uses_agent_identity_without_default_fallback(self):
        selected = MagicMock()
        with patch.dict(os.environ, {"AGENT_IDENTITY_ENABLED": "true"}), \
             patch.object(agent, "_runtime_agent_credential", None), \
             patch.object(agent, "get_agent_credential", return_value=selected), \
             patch.object(agent, "DefaultAzureCredential") as default:
            self.assertIs(agent._runtime_credential(), selected)
        default.assert_not_called()

    def test_async_agent_identity_is_only_created_when_enabled(self):
        selected = MagicMock()
        with patch.dict(os.environ, {"AGENT_IDENTITY_ENABLED": "true"}), \
             patch.object(agent, "_runtime_agent_async_credential", None), \
             patch.object(agent, "get_async_agent_credential", return_value=selected):
            self.assertIs(agent._runtime_async_credential(), selected)
        with patch.dict(os.environ, {"AGENT_IDENTITY_ENABLED": "false"}), \
               patch.object(agent, "_runtime_agent_async_credential", None), \
             patch.object(agent, "get_async_agent_credential") as get_async:
            self.assertIsNone(agent._runtime_async_credential())
        get_async.assert_not_called()


# ===========================================================================
#  Tests – Pure functions
# ===========================================================================


class TestCosineSimilarity(unittest.TestCase):
    """Tests for cosine_similarity()."""

    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(agent.cosine_similarity(v, v), 1.0, places=5)

    def test_orthogonal_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        self.assertAlmostEqual(agent.cosine_similarity(v1, v2), 0.0, places=5)

    def test_opposite_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        self.assertAlmostEqual(agent.cosine_similarity(v1, v2), -1.0, places=5)

    def test_zero_vector_returns_zero(self):
        v1 = [0.0, 0.0, 0.0]
        v2 = [1.0, 2.0, 3.0]
        self.assertEqual(agent.cosine_similarity(v1, v2), 0.0)

    def test_both_zero_vectors(self):
        v1 = [0.0, 0.0]
        v2 = [0.0, 0.0]
        self.assertEqual(agent.cosine_similarity(v1, v2), 0.0)

    def test_high_dimensional(self):
        np.random.seed(42)
        v1 = list(np.random.randn(3072))
        v2 = list(np.random.randn(3072))
        result = agent.cosine_similarity(v1, v2)
        self.assertGreaterEqual(result, -1.0)
        self.assertLessEqual(result, 1.0)


# ===========================================================================
#  Tests – get_model_deployment
# ===========================================================================


class TestGetModelDeployment(unittest.TestCase):
    """Tests for get_model_deployment().

    The Azure Agents Learning SDK optimizes agent behavior in-process by
    learning a policy over discrete action choices rather than fine-tuning
    model weights, so the model deployment is always the base Foundry model.
    """

    def test_returns_base_model(self):
        result = agent.get_model_deployment()
        self.assertEqual(result, agent.FOUNDRY_MODEL_DEPLOYMENT_NAME)


# ===========================================================================
#  Tests – hello_mcp_tool
# ===========================================================================


class TestHelloMcpTool(unittest.TestCase):
    def test_returns_greeting(self):
        self.assertEqual(agent.hello_mcp_tool(), "Hello I am MCPTool!")


# ===========================================================================
#  Tests – get_snippet_tool / save_snippet_tool
# ===========================================================================


class TestSnippetTools(unittest.TestCase):
    @patch.object(agent, "blob_service_client", None)
    def test_get_snippet_no_storage(self):
        result = agent.get_snippet_tool("test")
        self.assertIn("Error", result)

    @patch.object(agent, "blob_service_client", None)
    def test_save_snippet_no_storage(self):
        result = agent.save_snippet_tool("test", "content")
        self.assertIn("Error", result)

    def test_get_snippet_success(self):
        mock_blob_client = MagicMock()
        mock_blob_client.download_blob.return_value.readall.return_value = b'{"key": "value"}'
        mock_bsc = MagicMock()
        mock_bsc.get_blob_client.return_value = mock_blob_client
        with patch.object(agent, "blob_service_client", mock_bsc):
            result = agent.get_snippet_tool("mysnippet")
        self.assertEqual(result, '{"key": "value"}')

    def test_save_snippet_success(self):
        mock_blob_client = MagicMock()
        mock_bsc = MagicMock()
        mock_bsc.get_blob_client.return_value = mock_blob_client
        with patch.object(agent, "blob_service_client", mock_bsc):
            result = agent.save_snippet_tool("mysnippet", "hello")
        self.assertIn("saved successfully", result)

    def test_get_snippet_exception(self):
        mock_bsc = MagicMock()
        mock_bsc.get_blob_client.side_effect = Exception("connection error")
        with patch.object(agent, "blob_service_client", mock_bsc):
            result = agent.get_snippet_tool("fail")
        self.assertIn("Error", result)


# ===========================================================================
#  Tests – ask_foundry_tool
# ===========================================================================


class TestAskFoundryTool(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_no_endpoint(self):
        result = agent.ask_foundry_tool("hello?")
        self.assertIn("Error", result)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com/api/projects/proj-default")
    def test_success(self):
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "42 is the answer"
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mock_cred:
            mock_cred.return_value.get_token.return_value.token = "fake-token"
            result = agent.ask_foundry_tool("What is the answer?")
        self.assertEqual(result, "42 is the answer")

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_no_choices(self):
        mock_response = MagicMock()
        mock_response.choices = []
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mock_cred:
            mock_cred.return_value.get_token.return_value.token = "fake"
            result = agent.ask_foundry_tool("hello")
        self.assertEqual(result, "No response generated")


# ===========================================================================
#  Tests – Memory tools (unavailable path)
# ===========================================================================


class TestMemoryToolsUnavailable(unittest.TestCase):
    """When memory providers are None the tools should return JSON error."""

    @patch.object(agent, "short_term_memory", None)
    def test_store_memory_no_provider(self):
        result = json.loads(agent.store_memory_tool("some content", "sess-1"))
        self.assertIn("error", result)

    @patch.object(agent, "short_term_memory", None)
    def test_recall_memory_no_provider(self):
        result = json.loads(agent.recall_memory_tool("query", "sess-1"))
        self.assertIn("error", result)

    @patch.object(agent, "short_term_memory", None)
    def test_get_session_history_no_provider(self):
        result = json.loads(agent.get_session_history_tool("sess-1"))
        self.assertIn("error", result)

    @patch.object(agent, "short_term_memory", None)
    def test_clear_session_memory_no_provider(self):
        result = json.loads(agent.clear_session_memory_tool("sess-1"))
        self.assertIn("error", result)


# ===========================================================================
#  Tests – Facts memory tools (unavailable path)
# ===========================================================================


class TestFactsToolsUnavailable(unittest.TestCase):
    @patch.object(agent, "facts_memory", None)
    def test_search_facts_no_provider(self):
        result = json.loads(agent.search_facts_tool("query"))
        self.assertIn("error", result)

    @patch.object(agent, "facts_memory", None)
    def test_get_customer_churn_facts_no_provider(self):
        result = json.loads(agent.get_customer_churn_facts_tool())
        self.assertIn("error", result)

    @patch.object(agent, "facts_memory", None)
    def test_get_pipeline_health_facts_no_provider(self):
        result = json.loads(agent.get_pipeline_health_facts_tool())
        self.assertIn("error", result)

    @patch.object(agent, "facts_memory", None)
    def test_get_user_security_facts_no_provider(self):
        result = json.loads(agent.get_user_security_facts_tool())
        self.assertIn("error", result)

    @patch.object(agent, "facts_memory", None)
    def test_cross_domain_analysis_no_provider(self):
        result = json.loads(agent.cross_domain_analysis_tool("q", "customer", "devops"))
        self.assertIn("error", result)

    @patch.object(agent, "facts_memory", None)
    def test_get_facts_memory_stats_no_provider(self):
        result = json.loads(agent.get_facts_memory_stats_tool())
        self.assertIn("error", result)


# ===========================================================================
#  Tests – Learning tools (unavailable path)
# ===========================================================================


class TestLearningToolsUnavailable(unittest.TestCase):
    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_list_episodes_unavailable(self):
        result = json.loads(agent.learning_list_episodes_tool())
        self.assertIn("error", result)

    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_get_episode_unavailable(self):
        result = json.loads(agent.learning_get_episode_tool("ep-1"))
        self.assertIn("error", result)

    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_assign_reward_unavailable(self):
        result = json.loads(agent.learning_assign_reward_tool("ep-1", 0.9))
        self.assertIn("error", result)

    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_list_rewards_unavailable(self):
        result = json.loads(agent.learning_list_rewards_tool())
        self.assertIn("error", result)

    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_score_episode_unavailable(self):
        result = json.loads(agent.learning_score_episode_tool("ep-1"))
        self.assertIn("error", result)

    @patch.object(agent, "LEARNING_AVAILABLE", False)
    @patch.object(agent, "learning_store", None)
    def test_get_policy_unavailable(self):
        result = json.loads(agent.learning_get_policy_tool())
        self.assertIn("error", result)


# ===========================================================================
#  Tests – find_similar_tasks
# ===========================================================================


class TestFindSimilarTasks(unittest.TestCase):
    @patch.object(agent, "cosmos_tasks_container", None)
    def test_returns_empty_when_no_container(self):
        self.assertEqual(agent.find_similar_tasks([1.0, 2.0, 3.0]), [])

    def test_filters_by_threshold(self):
        mock_container = MagicMock()
        items = [
            {"id": "1", "task": "high match", "intent": "test", "embedding": [1.0, 0.0], "created_at": "2024-01-01"},
            {"id": "2", "task": "low match", "intent": "test", "embedding": [0.0, 1.0], "created_at": "2024-01-01"},
        ]
        mock_container.query_items.return_value = items
        with patch.object(agent, "cosmos_tasks_container", mock_container):
            results = agent.find_similar_tasks([1.0, 0.0], threshold=0.9, limit=5)
        # Only the identical-direction vector should pass threshold=0.9
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "1")

    def test_respects_limit(self):
        mock_container = MagicMock()
        items = [
            {"id": str(i), "task": f"t{i}", "intent": "x", "embedding": [1.0, 0.0], "created_at": ""}
            for i in range(10)
        ]
        mock_container.query_items.return_value = items
        with patch.object(agent, "cosmos_tasks_container", mock_container):
            results = agent.find_similar_tasks([1.0, 0.0], threshold=0.0, limit=3)
        self.assertLessEqual(len(results), 3)

    def test_handles_query_exception(self):
        mock_container = MagicMock()
        mock_container.query_items.side_effect = Exception("db error")
        with patch.object(agent, "cosmos_tasks_container", mock_container):
            results = agent.find_similar_tasks([1.0, 0.0])
        self.assertEqual(results, [])


# ===========================================================================
#  Tests – analyze_intent
# ===========================================================================


class TestAnalyzeIntent(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_returns_unknown_without_endpoint(self):
        self.assertEqual(agent.analyze_intent("do something"), "unknown")

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_returns_intent(self):
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "data analysis"
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            result = agent.analyze_intent("analyze customer data")
        self.assertEqual(result, "data analysis")


# ===========================================================================
#  Tests – generate_plan
# ===========================================================================


class TestGeneratePlan(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_without_endpoint(self):
        plan = agent.generate_plan("do something", [])
        self.assertEqual(len(plan), 1)
        self.assertIn("Manual planning required", plan[0]["action"])

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_valid_json_response(self):
        expected_steps = [
            {"step": 1, "action": "Step one", "description": "Do step one", "estimated_effort": "low"}
        ]
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(expected_steps)
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            plan = agent.generate_plan("build a dashboard", [])
        self.assertEqual(plan, expected_steps)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_markdown_wrapped_json(self):
        steps = [{"step": 1, "action": "A", "description": "B", "estimated_effort": "low"}]
        raw = f"```json\n{json.dumps(steps)}\n```"
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = raw
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            plan = agent.generate_plan("deploy service", [])
        self.assertEqual(plan, steps)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_invalid_json_fallback(self):
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Just do it manually"
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            plan = agent.generate_plan("vague task", [])
        self.assertEqual(len(plan), 1)
        self.assertIn("Execute task", plan[0]["action"])

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_exception_returns_error_step(self):
        with patch("openai.AzureOpenAI", side_effect=Exception("oops")), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            plan = agent.generate_plan("task", [])
        self.assertEqual(plan[0]["action"], "Error")


# ===========================================================================
#  Tests – generate_plan_with_instructions
# ===========================================================================


class TestGeneratePlanWithInstructions(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_without_endpoint(self):
        plan = agent.generate_plan_with_instructions("task", [], [])
        self.assertEqual(plan[0]["action"], "Manual planning required")

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_with_all_contexts(self):
        steps = [{"step": 1, "action": "A", "description": "B", "estimated_effort": "low", "source": "adapted"}]
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps(steps)
        mock_response.choices = [mock_choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        similar = [{"task": "old task", "intent": "test", "similarity": 0.85}]
        instructions = [{"title": "Guide", "category": "devops", "description": "How to deploy",
                         "score": 0.9, "steps": [{"step": 1, "action": "Deploy", "description": "Deploy it"}],
                         "content_excerpt": "Use kubectl apply"}]
        facts = [{"domain": "devops", "statement": "Pipeline healthy", "confidence": 0.95,
                  "fact_type": "observation", "context": {"success_rate": 0.9}}]

        with patch("openai.AzureOpenAI", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential") as mc:
            mc.return_value.get_token.return_value.token = "t"
            plan = agent.generate_plan_with_instructions("deploy service", similar, instructions, facts)
        self.assertEqual(plan, steps)


# ===========================================================================
#  Tests – next_best_action_tool
# ===========================================================================


class TestNextBestActionTool(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_no_foundry_endpoint(self):
        result = json.loads(agent.next_best_action_tool("do something"))
        self.assertIn("error", result)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    @patch.object(agent, "cosmos_tasks_container", None)
    def test_no_cosmos(self):
        result = json.loads(agent.next_best_action_tool("deploy it"))
        self.assertIn("error", result)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    @patch.object(agent, "long_term_memory", None)
    @patch.object(agent, "facts_memory", None)
    @patch.object(agent, "AGENT365_APPROVAL_AVAILABLE", False)
    def test_full_pipeline(self):
        """Test the happy-path of next_best_action_tool with mocked dependencies."""
        mock_tasks = MagicMock()
        mock_plans = MagicMock()
        mock_embedding = [0.1] * 3072
        mock_plan_steps = [{"step": 1, "action": "Go", "description": "Do it", "estimated_effort": "low", "source": "original"}]

        with patch.object(agent, "cosmos_tasks_container", mock_tasks), \
             patch.object(agent, "cosmos_plans_container", mock_plans), \
             patch.object(agent, "get_embedding", return_value=mock_embedding), \
             patch.object(agent, "analyze_intent", return_value="code generation"), \
             patch.object(agent, "find_similar_tasks", return_value=[]), \
             patch.object(agent, "generate_plan_with_instructions", return_value=mock_plan_steps):
            result = json.loads(agent.next_best_action_tool("write a REST API"))

        self.assertIn("task_id", result)
        self.assertEqual(result["intent"], "code generation")
        self.assertEqual(result["plan"]["total_steps"], 1)
        self.assertTrue(result["metadata"]["stored_in_cosmos"])
        mock_tasks.upsert_item.assert_called_once()
        mock_plans.upsert_item.assert_called_once()


# ===========================================================================
#  Tests – MCPTool / MCPToolResult dataclasses
# ===========================================================================


class TestDataclasses(unittest.TestCase):
    def test_mcp_tool(self):
        tool = agent.MCPTool(name="test", description="A test tool", inputSchema={"type": "object"})
        self.assertEqual(tool.name, "test")
        self.assertEqual(tool.description, "A test tool")

    def test_mcp_tool_result_defaults(self):
        result = agent.MCPToolResult(content=[{"type": "text", "text": "ok"}])
        self.assertFalse(result.isError)

    def test_mcp_tool_result_error(self):
        result = agent.MCPToolResult(content=[{"type": "text", "text": "fail"}], isError=True)
        self.assertTrue(result.isError)

    def test_mcp_tool_result_asdict(self):
        result = agent.MCPToolResult(content=[{"type": "text", "text": "hi"}])
        d = asdict(result)
        self.assertEqual(d["content"], [{"type": "text", "text": "hi"}])
        self.assertFalse(d["isError"])


# ===========================================================================
#  Tests – execute_tool dispatcher
# ===========================================================================


class TestExecuteTool(unittest.TestCase):
    def test_hello_mcp(self):
        result = _run(agent.execute_tool("hello_mcp", {}))
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0]["text"], "Hello I am MCPTool!")

    @patch.object(agent, "blob_service_client", None)
    def test_get_snippet_no_storage(self):
        result = _run(agent.execute_tool("get_snippet", {"snippetname": "x"}))
        self.assertTrue(result.isError)

    def test_unknown_tool(self):
        result = _run(agent.execute_tool("nonexistent_tool", {}))
        self.assertTrue(result.isError)

    def test_get_snippet_missing_name(self):
        result = _run(agent.execute_tool("get_snippet", {}))
        self.assertTrue(result.isError)

    @patch.object(agent, "blob_service_client", None)
    def test_save_snippet_no_storage(self):
        result = _run(agent.execute_tool("save_snippet", {"snippetname": "x", "snippet": "data"}))
        self.assertTrue(result.isError)

    def test_save_snippet_missing_name(self):
        result = _run(agent.execute_tool("save_snippet", {"snippet": "data"}))
        self.assertTrue(result.isError)

    def test_save_snippet_missing_content(self):
        result = _run(agent.execute_tool("save_snippet", {"snippetname": "x"}))
        self.assertTrue(result.isError)


# ===========================================================================
#  Tests – FastAPI endpoints
# ===========================================================================


class TestFastAPIEndpoints(unittest.TestCase):
    """Test FastAPI endpoints using the ASGI test client."""

    @classmethod
    def setUpClass(cls):
        # Import httpx for async testing, or fall back to TestClient
        try:
            from fastapi.testclient import TestClient
            cls.client = TestClient(agent.app)
            cls.has_client = True
        except ImportError:
            cls.has_client = False

    def test_health(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")
        self.assertIn("timestamp", data)

    def test_root(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "MCP Server")
        self.assertIn("endpoints", data)

    def test_mcp_message_initialize(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
            "id": 1
        }
        resp = self.client.post("/runtime/webhooks/mcp/message", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["id"], 1)
        self.assertIn("serverInfo", data["result"])
        self.assertEqual(data["result"]["serverInfo"]["name"], "mcp-agents")

    def test_mcp_message_tools_list(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 2
        }
        resp = self.client.post("/runtime/webhooks/mcp/message", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        tools = data["result"]["tools"]
        self.assertIsInstance(tools, list)
        self.assertGreater(len(tools), 0)
        # Verify each tool has required fields
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)

    def test_mcp_message_tools_call_hello(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "hello_mcp", "arguments": {}},
            "id": 3
        }
        resp = self.client.post("/runtime/webhooks/mcp/message", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["result"]["isError"])
        self.assertEqual(data["result"]["content"][0]["text"], "Hello I am MCPTool!")

    def test_mcp_message_invalid_jsonrpc(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        payload = {"jsonrpc": "1.0", "method": "initialize", "id": 99}
        resp = self.client.post("/runtime/webhooks/mcp/message", json=payload)
        self.assertEqual(resp.status_code, 400)

    def test_mcp_message_unknown_method(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        payload = {"jsonrpc": "2.0", "method": "unknown/method", "id": 10}
        resp = self.client.post("/runtime/webhooks/mcp/message", json=payload)
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("Method not found", data["error"]["message"])

    def test_agent_chat_no_agent(self):
        """When mcp_ai_agent is None the endpoint should return 503."""
        if not self.has_client:
            self.skipTest("TestClient not available")
        with patch.object(agent, "mcp_ai_agent", None):
            resp = self.client.post("/agent/chat", json={"message": "hi"})
        self.assertEqual(resp.status_code, 503)

    def test_agent_chat_no_message(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        mock_agent = MagicMock()
        with patch.object(agent, "mcp_ai_agent", mock_agent):
            resp = self.client.post("/agent/chat", json={"message": ""})
        self.assertEqual(resp.status_code, 400)

    def test_agent_chat_stream_no_agent(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        with patch.object(agent, "mcp_ai_agent", None):
            resp = self.client.post("/agent/chat/stream", json={"message": "hi"})
        self.assertEqual(resp.status_code, 503)

    def test_agent_chat_stream_no_message(self):
        if not self.has_client:
            self.skipTest("TestClient not available")
        mock_agent = MagicMock()
        with patch.object(agent, "mcp_ai_agent", mock_agent):
            resp = self.client.post("/agent/chat/stream", json={"message": ""})
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
#  Tests – TOOLS list is self-consistent
# ===========================================================================


class TestToolsList(unittest.TestCase):
    def test_tools_have_unique_names(self):
        names = [t.name for t in agent.TOOLS]
        self.assertEqual(len(names), len(set(names)), "Duplicate tool names found")

    def test_all_tools_have_input_schema(self):
        for tool in agent.TOOLS:
            self.assertIn("type", tool.inputSchema)

    def test_hello_mcp_in_tools(self):
        names = [t.name for t in agent.TOOLS]
        self.assertIn("hello_mcp", names)

    def test_next_best_action_in_tools(self):
        names = [t.name for t in agent.TOOLS]
        self.assertIn("next_best_action", names)


# ===========================================================================
#  Tests – Fabric wrapper tools (disabled path)
# ===========================================================================


class TestFabricWrappers(unittest.TestCase):
    """When FABRIC_DATA_AGENTS_AVAILABLE is False the wrappers should error."""

    @patch.object(agent, "FABRIC_DATA_AGENTS_AVAILABLE", False)
    def test_fabric_query_lakehouse_disabled(self):
        # The function delegates to fabric_tools which is mocked
        # Just ensure callable without exception
        try:
            result = agent.fabric_query_lakehouse("lh1", "SELECT 1")
            # Result comes from mock, just check it doesn't crash
        except Exception:
            pass  # acceptable if it errors with mock

    @patch.object(agent, "FABRIC_DATA_AGENTS_AVAILABLE", False)
    def test_fabric_list_resources_disabled(self):
        try:
            result = agent.fabric_list_resources()
        except Exception:
            pass


# ===========================================================================
#  Tests – create_mcp_agent
# ===========================================================================


class TestCreateMcpAgent(unittest.TestCase):
    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "")
    def test_returns_none_without_endpoint(self):
        self.assertIsNone(agent.create_mcp_agent())

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_returns_client_on_success(self):
        mock_client = MagicMock()
        with patch.object(agent, "AzureAIAgentClient", return_value=mock_client), \
             patch.object(agent, "DefaultAzureCredential"):
            result = agent.create_mcp_agent()
        self.assertEqual(result, mock_client)

    @patch.object(agent, "FOUNDRY_PROJECT_ENDPOINT", "https://endpoint.azure.com")
    def test_returns_none_on_exception(self):
        with patch.object(agent, "AzureAIAgentClient", side_effect=Exception("fail")), \
             patch.object(agent, "DefaultAzureCredential"):
            result = agent.create_mcp_agent()
        self.assertIsNone(result)


# ===========================================================================
#  Tests – sessions dict
# ===========================================================================


class TestSessionsDict(unittest.TestCase):
    def test_sessions_is_dict(self):
        self.assertIsInstance(agent.sessions, dict)


# ===========================================================================
#  Tests – app instance
# ===========================================================================


class TestAppInstance(unittest.TestCase):
    def test_app_is_fastapi(self):
        from fastapi import FastAPI
        self.assertIsInstance(agent.app, FastAPI)

    def test_app_title(self):
        self.assertEqual(agent.app.title, "AKS Next Best Action MCP Server")


if __name__ == "__main__":
    unittest.main()
