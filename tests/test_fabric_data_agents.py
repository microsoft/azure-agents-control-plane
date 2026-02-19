#!/usr/bin/env python3
"""
Unit tests for Fabric Data Agents and OneLake operations.

Tests cover:
- LakehouseAgent: intent detection, query execution, schema exploration
- WarehouseAgent: intent detection, T-SQL execution, schema exploration
- PipelineAgent: trigger, monitor, retry, list
- SemanticModelAgent: DAX/MDX queries, model discovery
- FabricAgentOrchestrator: routing, cross-domain queries
- OneLakeClient: list, read, write, delete, properties

All Fabric API calls are mocked to run without a live Fabric environment.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Dict, Any

# Ensure src is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Patch env vars before imports
os.environ["FABRIC_ENABLED"] = "true"
os.environ["FABRIC_DATA_AGENTS_ENABLED"] = "true"
os.environ["FABRIC_WORKSPACE_ID"] = "test-workspace-id-0001"
os.environ["FABRIC_API_ENDPOINT"] = "https://api.fabric.microsoft.com/v1"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _success_json(data: Dict[str, Any]) -> str:
    """Return a success JSON string matching fabric_*_tool output shape."""
    return json.dumps({"success": True, **data})


def _lakehouse_query_result(rows: int = 5) -> str:
    return _success_json({
        "lakehouse_id": "lh-001",
        "lakehouse_name": "TestLakehouse",
        "query": "SELECT * FROM test LIMIT 5",
        "results": {
            "schema": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "string"},
            ],
            "rows": [{"id": i, "name": f"row_{i}"} for i in range(rows)],
        },
    })


def _warehouse_query_result(rows: int = 5) -> str:
    return _success_json({
        "warehouse_id": "wh-001",
        "warehouse_name": "TestWarehouse",
        "query": "SELECT TOP 5 * FROM test",
        "results": {
            "schema": [
                {"name": "region", "type": "varchar"},
                {"name": "revenue", "type": "decimal"},
            ],
            "rows": [{"region": f"R{i}", "revenue": i * 1000} for i in range(rows)],
        },
    })


def _pipeline_trigger_result() -> str:
    return _success_json({
        "pipeline_id": "pl-001",
        "pipeline_name": "TestPipeline",
        "run_id": "run-abc-123",
        "status": "InProgress",
    })


def _pipeline_status_result(status: str = "Succeeded") -> str:
    return _success_json({
        "pipeline_id": "pl-001",
        "run_id": "run-abc-123",
        "status": status,
        "start_time": "2026-02-19T10:00:00Z",
        "end_time": "2026-02-19T10:05:00Z",
        "duration": "00:05:00",
    })


def _semantic_model_result() -> str:
    return _success_json({
        "dataset_id": "ds-001",
        "dataset_name": "KPIs",
        "query_language": "DAX",
        "results": {
            "schema": [{"name": "Measure", "type": "string"}],
            "rows": [{"Measure": "ChurnRate", "Value": 0.15}],
        },
    })


def _list_resources_result() -> str:
    return _success_json({
        "workspace_id": "test-workspace-id-0001",
        "resource_type": "all",
        "resources": {
            "lakehouses": [{"id": "lh-001", "name": "TestLakehouse"}],
            "warehouses": [{"id": "wh-001", "name": "TestWarehouse"}],
            "pipelines": [{"id": "pl-001", "name": "TestPipeline"}],
            "semantic_models": [{"id": "ds-001", "name": "TestModel"}],
        },
    })


# ===========================================================================
# Lakehouse Agent Tests
# ===========================================================================

class TestLakehouseAgent(unittest.TestCase):
    """Tests for LakehouseAgent."""

    def setUp(self):
        # Import inside setUp so env vars are picked up
        from fabric_agents import LakehouseAgent, AgentIntent
        self.AgentIntent = AgentIntent
        self.agent = LakehouseAgent()

    def test_detect_intent_query(self):
        intent = self.agent._detect_intent("show me all customers with high churn risk")
        self.assertEqual(intent, self.AgentIntent.QUERY_DATA)

    def test_detect_intent_schema(self):
        intent = self.agent._detect_intent("describe the schema of the sales table")
        self.assertEqual(intent, self.AgentIntent.EXPLORE_SCHEMA)

    def test_detect_intent_write(self):
        intent = self.agent._detect_intent("insert new records into the staging table")
        self.assertEqual(intent, self.AgentIntent.WRITE_DATA)

    def test_safe_limit_adds_limit(self):
        sql = "SELECT * FROM customers"
        result = self.agent._safe_limit(sql)
        self.assertIn("LIMIT", result)

    def test_safe_limit_preserves_existing(self):
        sql = "SELECT * FROM customers LIMIT 50"
        result = self.agent._safe_limit(sql)
        self.assertEqual(result, sql)

    @patch("fabric_agents.fabric_query_lakehouse_tool")
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_query(self, mock_list, mock_query):
        mock_list.return_value = _list_resources_result()
        mock_query.return_value = _lakehouse_query_result(3)

        resp = asyncio.run(self.agent.handle(
            "SELECT customer_id, name FROM customers LIMIT 3",
            lakehouse_id="lh-001",
            query="SELECT customer_id, name FROM customers LIMIT 3",
        ))

        self.assertTrue(resp.success)
        self.assertEqual(resp.agent_type, "lakehouse")
        self.assertIn("3 rows", resp.summary)
        mock_query.assert_called_once()

    @patch("fabric_agents.fabric_query_lakehouse_tool")
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_schema_explore(self, mock_list, mock_query):
        mock_list.return_value = _list_resources_result()
        tables_response = _success_json({
            "results": {
                "rows": [{"tableName": "customers"}, {"tableName": "orders"}],
            },
        })
        describe_response = _success_json({
            "results": {
                "rows": [
                    {"col_name": "id", "data_type": "int"},
                    {"col_name": "name", "data_type": "string"},
                ],
            },
        })
        mock_query.side_effect = [tables_response, describe_response, describe_response]

        resp = asyncio.run(self.agent.handle(
            "show me the schema of all tables",
            lakehouse_id="lh-001",
        ))

        self.assertTrue(resp.success)
        self.assertIn("2 tables", resp.summary)

    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_no_lakehouse(self, mock_list):
        mock_list.return_value = _success_json({
            "resources": {"lakehouses": []},
        })

        resp = asyncio.run(self.agent.handle("SELECT * FROM test"))

        self.assertFalse(resp.success)
        self.assertIn("No lakehouse", resp.summary)


# ===========================================================================
# Warehouse Agent Tests
# ===========================================================================

class TestWarehouseAgent(unittest.TestCase):
    """Tests for WarehouseAgent."""

    def setUp(self):
        from fabric_agents import WarehouseAgent, AgentIntent
        self.AgentIntent = AgentIntent
        self.agent = WarehouseAgent()

    def test_detect_intent_query(self):
        intent = self.agent._detect_intent("get top 10 sales by region")
        self.assertEqual(intent, self.AgentIntent.QUERY_DATA)

    def test_detect_intent_schema(self):
        intent = self.agent._detect_intent("show all tables in the warehouse schema")
        self.assertEqual(intent, self.AgentIntent.EXPLORE_SCHEMA)

    def test_ensure_top_adds_top(self):
        sql = "SELECT * FROM sales"
        result = self.agent._ensure_top(sql)
        self.assertIn("TOP 100", result)

    def test_ensure_top_preserves_existing(self):
        sql = "SELECT TOP 10 * FROM sales"
        result = self.agent._ensure_top(sql)
        self.assertEqual(result, sql)

    @patch("fabric_agents.fabric_query_warehouse_tool")
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_query(self, mock_list, mock_query):
        mock_list.return_value = _list_resources_result()
        mock_query.return_value = _warehouse_query_result(5)

        resp = asyncio.run(self.agent.handle(
            "SELECT TOP 5 region, revenue FROM sales",
            warehouse_id="wh-001",
            query="SELECT TOP 5 region, revenue FROM sales",
        ))

        self.assertTrue(resp.success)
        self.assertEqual(resp.agent_type, "warehouse")
        self.assertIn("5 rows", resp.summary)

    @patch("fabric_agents.fabric_query_warehouse_tool")
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_schema_explore(self, mock_list, mock_query):
        mock_list.return_value = _list_resources_result()
        tables_result = _success_json({
            "results": {
                "rows": [
                    {"TABLE_SCHEMA": "dbo", "TABLE_NAME": "sales", "TABLE_TYPE": "BASE TABLE"},
                ],
            },
        })
        columns_result = _success_json({
            "results": {
                "rows": [
                    {"COLUMN_NAME": "id", "DATA_TYPE": "int", "IS_NULLABLE": "NO", "CHARACTER_MAXIMUM_LENGTH": None},
                ],
            },
        })
        mock_query.side_effect = [tables_result, columns_result]

        resp = asyncio.run(self.agent.handle(
            "show me all tables and their columns",
            warehouse_id="wh-001",
        ))

        self.assertTrue(resp.success)
        self.assertIn("1 tables", resp.summary)


# ===========================================================================
# Pipeline Agent Tests
# ===========================================================================

class TestPipelineAgent(unittest.TestCase):
    """Tests for PipelineAgent."""

    def setUp(self):
        from fabric_agents import PipelineAgent, AgentIntent
        self.AgentIntent = AgentIntent
        self.agent = PipelineAgent()

    def test_detect_intent_trigger(self):
        intent = self.agent._detect_intent("trigger the ETL pipeline")
        self.assertEqual(intent, self.AgentIntent.TRIGGER_PIPELINE)

    def test_detect_intent_monitor(self):
        intent = self.agent._detect_intent("check the status of pipeline run abc123")
        self.assertEqual(intent, self.AgentIntent.MONITOR_PIPELINE)

    def test_detect_intent_retry(self):
        intent = self.agent._detect_intent("retry the failed pipeline")
        self.assertEqual(intent, self.AgentIntent.RETRY_PIPELINE)

    def test_detect_intent_list(self):
        intent = self.agent._detect_intent("list all available pipelines")
        self.assertEqual(intent, self.AgentIntent.LIST_RESOURCES)

    @patch("fabric_agents.fabric_trigger_pipeline_tool")
    def test_handle_trigger(self, mock_trigger):
        mock_trigger.return_value = _pipeline_trigger_result()

        resp = asyncio.run(self.agent.handle(
            "trigger the churn prediction pipeline",
            pipeline_id="pl-001",
            pipeline_name="ChurnPredictionETL",
        ))

        self.assertTrue(resp.success)
        self.assertIn("run-abc-123", resp.summary)
        mock_trigger.assert_called_once()

    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    def test_handle_monitor(self, mock_status):
        mock_status.return_value = _pipeline_status_result("Succeeded")

        resp = asyncio.run(self.agent.handle(
            "check pipeline status",
            pipeline_id="pl-001",
            run_id="run-abc-123",
        ))

        self.assertTrue(resp.success)
        self.assertIn("Succeeded", resp.summary)

    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_list(self, mock_list):
        mock_list.return_value = _list_resources_result()

        resp = asyncio.run(self.agent.handle("list all pipelines"))

        self.assertTrue(resp.success)
        self.assertIn("1 data pipelines", resp.summary)

    def test_handle_trigger_missing_id(self):
        resp = asyncio.run(self.agent.handle(
            "trigger something",
        ))
        self.assertFalse(resp.success)
        self.assertIn("pipeline_id", resp.summary)


# ===========================================================================
# Semantic Model Agent Tests
# ===========================================================================

class TestSemanticModelAgent(unittest.TestCase):
    """Tests for SemanticModelAgent."""

    def setUp(self):
        from fabric_agents import SemanticModelAgent, AgentIntent
        self.AgentIntent = AgentIntent
        self.agent = SemanticModelAgent()

    def test_detect_intent_query(self):
        intent = self.agent._detect_intent("evaluate this DAX expression")
        self.assertEqual(intent, self.AgentIntent.QUERY_DATA)

    def test_detect_intent_kpi(self):
        intent = self.agent._detect_intent("show me the churn rate KPI from the dashboard")
        self.assertEqual(intent, self.AgentIntent.QUERY_KPI)

    def test_detect_intent_list(self):
        intent = self.agent._detect_intent("list available semantic models")
        self.assertEqual(intent, self.AgentIntent.LIST_RESOURCES)

    @patch("fabric_agents.fabric_query_semantic_model_tool")
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_query(self, mock_list, mock_query):
        mock_list.return_value = _list_resources_result()
        mock_query.return_value = _semantic_model_result()

        resp = asyncio.run(self.agent.handle(
            "EVALUATE ALL(Customers)",
            dataset_id="ds-001",
            query="EVALUATE ALL(Customers)",
        ))

        self.assertTrue(resp.success)
        self.assertEqual(resp.agent_type, "semantic_model")
        mock_query.assert_called_once()

    @patch("fabric_agents.fabric_list_resources_tool")
    def test_handle_list_models(self, mock_list):
        mock_list.return_value = _list_resources_result()

        resp = asyncio.run(self.agent.handle("show available models"))

        self.assertTrue(resp.success)
        self.assertIn("1 semantic models", resp.summary)


# ===========================================================================
# Orchestrator Tests
# ===========================================================================

class TestFabricAgentOrchestrator(unittest.TestCase):
    """Tests for FabricAgentOrchestrator."""

    def setUp(self):
        from fabric_agents import FabricAgentOrchestrator, LakehouseAgent, WarehouseAgent, PipelineAgent, SemanticModelAgent
        self.orchestrator = FabricAgentOrchestrator()

    def test_route_lakehouse(self):
        agent, _ = self.orchestrator.route("query the lakehouse for customer data")
        self.assertIsInstance(agent, type(self.orchestrator.lakehouse_agent))

    def test_route_warehouse(self):
        agent, _ = self.orchestrator.route("run a t-sql query on the data warehouse")
        self.assertIsInstance(agent, type(self.orchestrator.warehouse_agent))

    def test_route_pipeline(self):
        agent, _ = self.orchestrator.route("trigger the ETL pipeline")
        self.assertIsInstance(agent, type(self.orchestrator.pipeline_agent))

    def test_route_semantic_model(self):
        agent, _ = self.orchestrator.route("show KPIs from the Power BI dashboard")
        self.assertIsInstance(agent, type(self.orchestrator.semantic_model_agent))

    def test_route_explicit_agent_type(self):
        agent, _ = self.orchestrator.route("do something", agent_type="warehouse")
        self.assertIsInstance(agent, type(self.orchestrator.warehouse_agent))

    def test_route_by_resource_id(self):
        agent, kwargs = self.orchestrator.route("do something", pipeline_id="pl-001")
        self.assertIsInstance(agent, type(self.orchestrator.pipeline_agent))

    def test_route_default_lakehouse(self):
        agent, _ = self.orchestrator.route("hello how are you")
        self.assertIsInstance(agent, type(self.orchestrator.lakehouse_agent))

    @patch("fabric_agents.fabric_list_resources_tool")
    def test_list_all_resources(self, mock_list):
        mock_list.return_value = _list_resources_result()

        resp = asyncio.run(self.orchestrator.list_all_resources())

        self.assertTrue(resp.success)
        self.assertEqual(resp.agent_type, "orchestrator")
        self.assertIn("lakehouses", resp.data)

    @patch("fabric_agents.FABRIC_DATA_AGENTS_ENABLED", False)
    def test_handle_disabled(self):
        resp = asyncio.run(self.orchestrator.handle("query data"))
        self.assertFalse(resp.success)
        self.assertIn("not enabled", resp.summary)


# ===========================================================================
# OneLake Client Tests
# ===========================================================================

class TestOneLakeClient(unittest.TestCase):
    """Tests for OneLakeClient and tool wrappers."""

    def setUp(self):
        from fabric_onelake import OneLakeClient
        self.client = OneLakeClient(
            workspace_id="test-workspace-id-0001",
            dfs_endpoint="https://onelake.dfs.fabric.microsoft.com",
        )

    def test_build_url_files(self):
        url = self.client._build_url("lh-001", "raw/data.csv", "Files")
        self.assertEqual(
            url,
            "https://onelake.dfs.fabric.microsoft.com/test-workspace-id-0001/lh-001/Files/raw/data.csv",
        )

    def test_build_url_tables(self):
        url = self.client._build_url("lh-001", "customers", "Tables")
        self.assertEqual(
            url,
            "https://onelake.dfs.fabric.microsoft.com/test-workspace-id-0001/lh-001/Tables/customers",
        )

    @patch("fabric_onelake.requests.get")
    @patch.object(type(MagicMock()), "_get_token", return_value="fake-token")
    def test_list_files(self, _mock_token, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "paths": [
                {"name": "Files/raw/data.csv", "isDirectory": "false", "contentLength": "1024", "lastModified": "2026-02-19"},
                {"name": "Files/processed", "isDirectory": "true", "contentLength": "0", "lastModified": "2026-02-18"},
            ]
        }
        mock_get.return_value = mock_resp

        # Patch _get_token on the instance
        self.client._get_token = MagicMock(return_value="fake-token")
        entries = self.client.list_files("lh-001", "raw")

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "data.csv")
        self.assertTrue(entries[1]["isDirectory"])

    @patch("fabric_onelake.requests.head")
    @patch("fabric_onelake.requests.get")
    def test_read_file_text(self, mock_get, mock_head):
        self.client._get_token = MagicMock(return_value="fake-token")

        mock_head_resp = MagicMock()
        mock_head_resp.raise_for_status = MagicMock()
        mock_head_resp.headers = {"Content-Length": "100"}
        mock_head.return_value = mock_head_resp

        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.headers = {"Content-Type": "text/csv"}
        mock_get_resp.content = b"id,name\n1,Alice\n2,Bob"
        mock_get.return_value = mock_get_resp

        result = self.client.read_file("lh-001", "data.csv")

        self.assertTrue(result["success"])
        self.assertTrue(result["isText"])
        self.assertIn("Alice", result["content"])

    @patch("fabric_onelake.requests.put")
    @patch("fabric_onelake.requests.patch")
    def test_write_file(self, mock_patch, mock_put):
        self.client._get_token = MagicMock(return_value="fake-token")

        # All responses succeed
        for mock in [mock_put, mock_patch]:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            mock.return_value = resp

        result = self.client.write_file("lh-001", "output/result.csv", "id,value\n1,100")

        self.assertTrue(result["success"])
        self.assertEqual(result["size"], len("id,value\n1,100".encode("utf-8")))
        # Create (PUT) + Append (PATCH) + Flush (PATCH)
        mock_put.assert_called_once()
        self.assertEqual(mock_patch.call_count, 2)

    @patch("fabric_onelake.requests.delete")
    def test_delete_file(self, mock_delete):
        self.client._get_token = MagicMock(return_value="fake-token")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_delete.return_value = mock_resp

        result = self.client.delete_file("lh-001", "temp/old.csv")

        self.assertTrue(result["success"])
        self.assertTrue(result["deleted"])

    @patch("fabric_onelake.requests.head")
    def test_get_file_properties(self, mock_head):
        self.client._get_token = MagicMock(return_value="fake-token")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {
            "Content-Length": "2048",
            "Content-Type": "text/csv",
            "Last-Modified": "Wed, 19 Feb 2026 10:00:00 GMT",
            "ETag": "\"abcdef123\"",
        }
        mock_head.return_value = mock_resp

        result = self.client.get_file_properties("lh-001", "data.csv")

        self.assertTrue(result["success"])
        self.assertEqual(result["size"], 2048)
        self.assertEqual(result["contentType"], "text/csv")


# ===========================================================================
# OneLake Tool Wrappers Tests
# ===========================================================================

class TestOneLakeTools(unittest.TestCase):
    """Tests for OneLake MCP tool wrappers."""

    @patch("fabric_onelake.get_onelake_client")
    def test_onelake_list_files_tool(self, mock_get_client):
        from fabric_onelake import onelake_list_files_tool

        mock_client = MagicMock()
        mock_client.list_files.return_value = [
            {"name": "data.csv", "path": "Files/data.csv", "isDirectory": False, "contentLength": 100, "lastModified": "2026-02-19"},
        ]
        mock_get_client.return_value = mock_client

        result = json.loads(onelake_list_files_tool("lh-001", ""))

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["files"][0]["name"], "data.csv")

    @patch("fabric_onelake.get_onelake_client")
    def test_onelake_write_file_tool(self, mock_get_client):
        from fabric_onelake import onelake_write_file_tool

        mock_client = MagicMock()
        mock_client.write_file.return_value = {
            "success": True,
            "path": "output.json",
            "size": 42,
        }
        mock_get_client.return_value = mock_client

        result = json.loads(onelake_write_file_tool("lh-001", "output.json", '{"key": "value"}'))

        self.assertTrue(result["success"])

    @patch("fabric_onelake.get_onelake_client")
    def test_onelake_read_file_tool_error(self, mock_get_client):
        from fabric_onelake import onelake_read_file_tool

        mock_client = MagicMock()
        mock_client.read_file.side_effect = Exception("Network timeout")
        mock_get_client.return_value = mock_client

        result = json.loads(onelake_read_file_tool("lh-001", "missing.csv"))

        self.assertFalse(result["success"])
        self.assertIn("Network timeout", result["error"])


# ===========================================================================
# Agent Response Tests
# ===========================================================================

class TestAgentResponse(unittest.TestCase):
    """Tests for AgentResponse serialization."""

    def test_to_json(self):
        from fabric_agents import AgentResponse

        resp = AgentResponse(
            success=True,
            agent_type="lakehouse",
            intent="query_data",
            summary="Returned 5 rows",
            data={"rows": 5},
            actions_taken=["Executed query"],
            recommendations=["Use LIMIT"],
        )

        j = json.loads(resp.to_json())
        self.assertTrue(j["success"])
        self.assertEqual(j["agent_type"], "lakehouse")
        self.assertIn("timestamp", j)


if __name__ == "__main__":
    unittest.main(verbosity=2)
