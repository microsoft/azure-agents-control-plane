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
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any

import requests

# Ensure src is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

def setUpModule():
    global _modules_patch, _network_patch
    _modules_patch = patch.dict(sys.modules)
    _modules_patch.start()
    for name in ("fabric_tools", "fabric_agents", "fabric_onelake"):
        sys.modules.pop(name, None)
    with patch.dict(os.environ, {
        "FABRIC_ENABLED": "true",
        "FABRIC_DATA_AGENTS_ENABLED": "true",
        "FABRIC_WORKSPACE_ID": "test-workspace-id-0001",
        "FABRIC_API_ENDPOINT": "https://api.fabric.microsoft.com/v1",
        "FABRIC_ONELAKE_DFS_ENDPOINT": "https://onelake.dfs.fabric.microsoft.com",
    }):
        __import__("fabric_agents")
        __import__("fabric_onelake")
    _network_patch = patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("Unexpected live HTTP request in Fabric unit tests"),
    )
    _network_patch.start()


def tearDownModule():
    _network_patch.stop()
    _modules_patch.stop()


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

    def test_safe_limit_does_not_modify_writes(self):
        sql = "INSERT INTO customers VALUES (1, 'Alice')"
        self.assertEqual(self.agent._safe_limit(sql), sql)

    def test_sql_column_names_do_not_trigger_schema_exploration(self):
        self.assertEqual(
            self.agent._detect_intent("SELECT schema, columns FROM metadata"),
            self.AgentIntent.QUERY_DATA,
        )

    @patch("fabric_agents.fabric_query_lakehouse_tool")
    def test_explicit_query_takes_precedence_over_natural_language(self, mock_query):
        mock_query.return_value = _lakehouse_query_result(1)
        response = asyncio.run(self.agent.handle(
            "show the columns for these customers",
            lakehouse_id="lh-001", query="SELECT columns FROM customers LIMIT 1",
        ))
        self.assertTrue(response.success)
        self.assertEqual(response.intent, "query_data")
        self.assertEqual(mock_query.call_args.args[1], "SELECT columns FROM customers LIMIT 1")

    @patch("fabric_agents.fabric_query_lakehouse_tool")
    def test_schema_failure_is_not_success(self, mock_query):
        mock_query.return_value = json.dumps({"success": False, "error": "Access denied"})
        response = asyncio.run(self.agent.handle("show tables", lakehouse_id="lh-001"))
        self.assertFalse(response.success)
        self.assertEqual(response.errors, ["Access denied"])
        mock_query.assert_called_once()

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

    def test_ensure_top_preserves_distinct_and_parenthesized_top(self):
        self.assertEqual(
            self.agent._ensure_top("SELECT DISTINCT region FROM sales"),
            "SELECT DISTINCT TOP 100 region FROM sales",
        )
        sql = "SELECT TOP (10) region FROM sales"
        self.assertEqual(self.agent._ensure_top(sql), sql)

    @patch("fabric_agents.fabric_query_warehouse_tool")
    def test_schema_column_failure_is_not_success(self, mock_query):
        mock_query.side_effect = [
            _success_json({"results": {"rows": [{"TABLE_NAME": "sales", "TABLE_SCHEMA": "dbo"}]}}),
            json.dumps({"success": False, "error": "Column lookup failed"}),
        ]
        response = asyncio.run(self.agent.handle("show tables", warehouse_id="wh-001"))
        self.assertFalse(response.success)
        self.assertEqual(response.errors, ["Column lookup failed"])

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

    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    def test_monitor_intent_is_not_overridden_by_an_unrelated_sql_query(self, mock_status):
        mock_status.return_value = _pipeline_status_result("Succeeded")
        response = asyncio.run(self.agent.handle(
            "check pipeline run status", pipeline_id="pl-001", run_id="run-abc-123",
            query="SELECT id FROM customers",
        ))
        self.assertTrue(response.success)
        self.assertEqual(response.intent, "monitor_pipeline")
        mock_status.assert_called_once()

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

    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    @patch("fabric_agents.fabric_trigger_pipeline_tool")
    def test_wait_reports_terminal_failure(self, mock_trigger, mock_status):
        mock_trigger.return_value = _pipeline_trigger_result()
        for status in ("Succeeded", "Failed", "Cancelled"):
            with self.subTest(status=status):
                mock_status.return_value = _pipeline_status_result(status)
                response = asyncio.run(self.agent.handle(
                    "trigger pipeline", pipeline_id="pl-001", wait_for_completion=True,
                ))
                self.assertEqual(response.success, status == "Succeeded")
                self.assertEqual(response.data["final_status"]["status"], status)
                if status != "Succeeded":
                    self.assertTrue(response.errors)

    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    def test_poll_stops_on_api_failure(self, mock_status):
        mock_status.return_value = json.dumps({"success": False, "error": "Forbidden"})
        response = asyncio.run(self.agent._poll_until_complete("pl-001", "run-001", ""))
        self.assertFalse(response["success"])
        mock_status.assert_called_once()

    @patch("fabric_agents.FABRIC_PIPELINE_TIMEOUT", 1)
    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    def test_poll_timeout_is_explicit(self, mock_status):
        mock_status.return_value = _pipeline_status_result("InProgress")
        clock = MagicMock()
        clock.monotonic.side_effect = [0, 0, 2, 2]
        with patch("fabric_agents.time", clock):
            response = asyncio.run(self.agent._poll_until_complete("pl-001", "run-001", ""))
        self.assertFalse(response["success"])
        self.assertEqual(response["status"], "Timeout")

    @patch("fabric_agents.FABRIC_AGENT_MAX_RETRIES", 3)
    @patch("fabric_agents.fabric_get_pipeline_status_tool")
    @patch("fabric_agents.fabric_trigger_pipeline_tool")
    def test_retry_is_bounded(self, mock_trigger, mock_status):
        mock_trigger.return_value = _pipeline_trigger_result()
        mock_status.return_value = _pipeline_status_result("Failed")
        response = asyncio.run(self.agent.handle("retry pipeline", pipeline_id="pl-001"))
        self.assertFalse(response.success)
        self.assertEqual(mock_trigger.call_count, 3)
        self.assertEqual(mock_status.call_count, 3)


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

    def test_resource_id_takes_precedence_over_keywords(self):
        selected, _ = self.orchestrator.route(
            "query lakehouse import history", warehouse_id="wh-001",
        )
        self.assertIs(selected, self.orchestrator.warehouse_agent)

    def test_invalid_agent_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown Fabric agent_type"):
            self.orchestrator.route("query data", agent_type="unknown")

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

    @patch("fabric_agents.FABRIC_DATA_AGENTS_ENABLED", False)
    @patch("fabric_agents.fabric_list_resources_tool")
    def test_list_and_cross_domain_are_also_disabled(self, mock_list):
        for response in (
            asyncio.run(self.orchestrator.list_all_resources()),
            asyncio.run(self.orchestrator.cross_domain_query("trigger pipeline")),
        ):
            self.assertFalse(response.success)
            self.assertIn("not enabled", response.summary)
        mock_list.assert_not_called()

    def test_cross_domain_passes_per_agent_queries_and_aggregates_failures(self):
        from fabric_agents import AgentResponse
        lakehouse = self.orchestrator.lakehouse_agent
        warehouse = self.orchestrator.warehouse_agent
        lakehouse.handle = AsyncMock(return_value=AgentResponse(True, "lakehouse", "query_data", "ok"))
        warehouse.handle = AsyncMock(return_value=AgentResponse(False, "warehouse", "query_data", "failed"))
        queries = {"lakehouse": "SELECT id FROM customers", "warehouse": "SELECT id FROM orders"}
        response = asyncio.run(self.orchestrator.cross_domain_query(
            "compare results", lakehouse_id="lh-001", warehouse_id="wh-001", queries=queries,
        ))
        self.assertFalse(response.success)
        self.assertEqual(len(response.data["sub_results"]), 2)
        self.assertEqual(lakehouse.handle.call_args.kwargs["query"], queries["lakehouse"])
        self.assertEqual(warehouse.handle.call_args.kwargs["query"], queries["warehouse"])


# ===========================================================================
# OneLake Client Tests
# ===========================================================================

class TestOneLakeClient(unittest.TestCase):
    """Tests for OneLakeClient and tool wrappers."""

    def setUp(self):
        from fabric_onelake import OneLakeClient
        credential = patch("fabric_onelake.DefaultAzureCredential")
        credential.start()
        self.addCleanup(credential.stop)
        self.client = OneLakeClient(
            workspace_id="test-workspace-id-0001",
            dfs_endpoint="https://onelake.dfs.fabric.microsoft.com",
        )
        self.client._get_token = MagicMock(return_value="fake-token")

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
    def test_list_files(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
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
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://onelake.dfs.fabric.microsoft.com/test-workspace-id-0001",
        )
        self.assertEqual(mock_get.call_args.kwargs["params"]["directory"], "lh-001/Files/raw")

    @patch("fabric_onelake.requests.get")
    def test_list_files_follows_continuation_and_scopes_root(self, mock_get):
        first = MagicMock(headers={"x-ms-continuation": "next/page+token"})
        first.json.return_value = {"paths": [{"name": "lh-001/Files/a.csv"}]}
        second = MagicMock(headers={})
        second.json.return_value = {"paths": [{"name": "lh-001/Files/b.csv", "isDirectory": False}]}
        mock_get.side_effect = [first, second]
        result = self.client.list_files("lh-001")
        self.assertEqual(len(result), 2)
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["directory"], "lh-001/Files")
        self.assertNotIn("continuation", mock_get.call_args_list[0].kwargs["params"])
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"]["continuation"], "next/page+token")

    def test_build_url_encodes_reserved_characters(self):
        self.assertTrue(self.client._build_url("lh-001", "raw/a b#c?.csv").endswith(
            "/lh-001/Files/raw/a%20b%23c%3F.csv"
        ))

    def test_invalid_paths_and_sections_fail_before_http(self):
        for path, section in (("", "Files"), ("../other.csv", "Files"), ("a.csv", "Other")):
            with self.subTest(path=path, section=section), self.assertRaises(ValueError):
                self.client.delete_file("lh-001", path, section)
        self.client._get_token.assert_not_called()

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
        mock_get_resp.iter_content.return_value = [mock_get_resp.content]
        mock_get_resp.__enter__.return_value = mock_get_resp
        mock_get.return_value = mock_get_resp

        result = self.client.read_file("lh-001", "data.csv")

        self.assertTrue(result["success"])
        self.assertTrue(result["isText"])
        self.assertIn("Alice", result["content"])
        self.assertEqual(result["size"], len(mock_get_resp.content))
        self.assertTrue(mock_get.call_args.kwargs["stream"])

    @patch("fabric_onelake.MAX_DOWNLOAD_BYTES", 4)
    @patch("fabric_onelake.requests.head")
    @patch("fabric_onelake.requests.get")
    def test_read_enforces_actual_download_limit(self, mock_get, mock_head):
        mock_head.return_value.headers = {"Content-Length": "1"}
        response = MagicMock(headers={"Content-Type": "text/plain"})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"1234", b"5", b"not-read"]
        mock_get.return_value = response
        with self.assertRaisesRegex(ValueError, "download limit"):
            self.client.read_file("lh-001", "data.txt")
        response.__exit__.assert_called_once()

    @patch("fabric_onelake.MAX_DOWNLOAD_BYTES", 4)
    @patch("fabric_onelake.requests.head")
    @patch("fabric_onelake.requests.get")
    def test_read_accepts_exact_limit(self, mock_get, mock_head):
        mock_head.return_value.headers = {"Content-Length": "4"}
        response = MagicMock(headers={"Content-Type": "text/plain"})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"12", b"34"]
        mock_get.return_value = response
        result = self.client.read_file("lh-001", "data.txt")
        self.assertEqual(result["content"], "1234")
        self.assertEqual(result["size"], 4)

    @patch("fabric_onelake.MAX_DOWNLOAD_BYTES", 4)
    @patch("fabric_onelake.requests.head")
    @patch("fabric_onelake.requests.get")
    def test_read_rejects_oversized_head_before_get(self, mock_get, mock_head):
        mock_head.return_value.headers = {"Content-Length": "5"}
        with self.assertRaisesRegex(ValueError, "File too large"):
            self.client.read_file("lh-001", "data.txt")
        mock_get.assert_not_called()

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

    @patch("fabric_onelake.requests.patch")
    @patch("fabric_onelake.requests.put")
    def test_write_without_overwrite_is_conditional(self, mock_put, mock_patch):
        self.client.write_file("lh-001", "data.txt", "text", overwrite=False)
        self.assertEqual(mock_put.call_args.kwargs["headers"]["If-None-Match"], "*")
        self.assertNotIn("mode", mock_put.call_args.kwargs["params"])
        self.assertNotIn("If-None-Match", mock_patch.call_args_list[0].kwargs["headers"])

    @patch("fabric_onelake.requests.patch")
    @patch("fabric_onelake.requests.put")
    def test_existing_file_is_not_appended_when_create_fails(self, mock_put, mock_patch):
        mock_put.return_value.raise_for_status.side_effect = requests.HTTPError("Precondition failed")
        with self.assertRaises(requests.HTTPError):
            self.client.write_file("lh-001", "data.txt", "text", overwrite=False)
        mock_patch.assert_not_called()

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
        mock_client.read_file.side_effect = requests.Timeout("Network timeout")
        mock_get_client.return_value = mock_client

        result = json.loads(onelake_read_file_tool("lh-001", "missing.csv"))

        self.assertFalse(result["success"])
        self.assertIn("Network timeout", result["error"])

    @patch("fabric_onelake.FABRIC_DATA_AGENTS_ENABLED", False)
    @patch("fabric_onelake.OneLakeClient")
    def test_disabled_tools_do_not_construct_a_client(self, mock_client):
        from fabric_onelake import onelake_list_files_tool
        result = json.loads(onelake_list_files_tool("lh-001"))
        self.assertFalse(result["success"])
        self.assertIn("not enabled", result["error"])
        mock_client.assert_not_called()


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


class TestFabricProvisioning(unittest.TestCase):
    """Execute the real PowerShell script with strictly in-memory service boundaries."""

    def run_provisioning(self, case):
        shell = shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell 7 is not available")
        root = Path(__file__).resolve().parents[1]
        command = r'''
$ErrorActionPreference = 'Stop'
$global:FabricUnitCalls = @()
function az {
    if ($args[0] -ne 'account' -or $args[1] -ne 'get-access-token') { throw 'Unexpected az command' }
    $global:LASTEXITCODE = 0
    return 'offline-token'
}
function azd {
    $global:LASTEXITCODE = 0
    if ($args[0] -ne 'env') { throw 'Unexpected azd command' }
    if ($args[1] -eq 'get-values') {
        if ($env:FABRIC_UNIT_CASE -eq 'lookup') { return 'FABRIC_CAPACITY_NAME="unit-capacity"' }
        return 'FABRIC_CAPACITY_NAME=""'
    }
    if ($args[1] -ne 'set' -or $args[-1] -ne 'offline') { throw 'Unexpected azd environment write' }
    if ($args[2] -eq 'FABRIC_DATA_AGENTS_ENABLED') { throw 'Provisioning must not enable the runtime' }
    if ($env:FABRIC_UNIT_CASE -eq 'save-failure') { $global:LASTEXITCODE = 1 }
}
function Start-Sleep { param($Seconds) }
function Invoke-RestMethod {
    [CmdletBinding()]
    param($Method, $Uri, $Headers, $Body, $TimeoutSec, $MaximumRedirection,
          $ResponseHeadersVariable, $StatusCodeVariable)
    $global:FabricUnitCalls += "$Method $Uri"
    $code = 200
    $responseHeaders = @{}
    $base = 'https://api.fabric.microsoft.com/v1'
    switch ("$Method $Uri") {
        "GET $base/capacities" {
            $result = @{ value = @(@{ id = 'cap-001'; displayName = 'unit-capacity' }) }
        }
        "GET $base/workspaces" {
            if ($env:FABRIC_UNIT_CASE -eq 'capacity-failure') {
                $result = @{ value = @(@{ id = 'ws-001'; displayName = 'unit-workspace'; capacityId = 'old-capacity' }) }
            } elseif ($env:FABRIC_UNIT_CASE -eq 'pagination') {
                $result = @{ value = @(); continuationUri = "$base/workspaces?continuation=next" }
            } else {
                $result = @{ value = @() }
            }
        }
        "GET $base/workspaces?continuation=next" {
            $result = @{ value = @(@{ id = 'ws-001'; displayName = 'unit-workspace'; capacityId = 'cap-001' }) }
        }
        "POST $base/workspaces" {
            $result = if ($env:FABRIC_UNIT_CASE -eq 'missing-id') { @{} } else { @{ id = 'ws-001' } }
        }
        "POST $base/workspaces/ws-001/assignToCapacity" { throw 'Capacity assignment failed' }
        "GET $base/workspaces/ws-001/lakehouses" { $result = @{ value = @() } }
        "GET $base/workspaces/ws-001/warehouses" { $result = @{ value = @() } }
        "POST $base/workspaces/ws-001/items" {
            $item = $Body | ConvertFrom-Json
            if ($item.type -eq 'Lakehouse' -and $env:FABRIC_UNIT_CASE -in @('lro', 'operation-failure')) {
                $code = 202
                $responseHeaders = @{ Location = "$base/operations/op-001"; 'Retry-After' = '1' }
                $result = $null
            } else {
                $result = @{ id = $(if ($item.type -eq 'Lakehouse') { 'lh-001' } else { 'wh-001' }) }
            }
        }
        "GET $base/operations/op-001" {
            $result = if ($env:FABRIC_UNIT_CASE -eq 'operation-failure') {
                @{ status = 'Failed'; error = @{ message = 'Item creation failed' } }
            } else {
                @{ status = 'Succeeded' }
            }
        }
        "GET $base/operations/op-001/result" { $result = @{ id = 'lh-001' } }
        default { throw "Unexpected HTTP request: $Method $Uri" }
    }
    if ($ResponseHeadersVariable) { Set-Variable -Name $ResponseHeadersVariable -Value $responseHeaders -Scope 1 }
    if ($StatusCodeVariable) { Set-Variable -Name $StatusCodeVariable -Value $code -Scope 1 }
    return $result
}
$options = @{ WorkspaceName = 'unit-workspace'; Environment = 'offline' }
if ($env:FABRIC_UNIT_CASE -ne 'lookup') { $options.CapacityId = 'cap-001' }
& $env:FABRIC_UNIT_SCRIPT @options
foreach ($call in $global:FabricUnitCalls) { Write-Host "CALL: $call" }
'''
        return subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=root,
            env={**os.environ, "FABRIC_UNIT_CASE": case,
                 "FABRIC_UNIT_SCRIPT": str(root / "scripts" / "deploy-fabric-workspace.ps1")},
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )

    def test_creation_and_resource_ids(self):
        result = self.run_provisioning("create")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Provisioning Complete!", result.stdout)
        self.assertIn("FABRIC_LAKEHOUSE_ID = lh-001", result.stdout)
        self.assertIn("CALL: GET https://api.fabric.microsoft.com/v1/workspaces", result.stdout)
        self.assertNotIn("CALL: POST https://api.fabric.microsoft.com/v1/workspaces/ws-001/assignToCapacity", result.stdout)

    def test_long_running_item_creation_fetches_result(self):
        result = self.run_provisioning("lro")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALL: GET https://api.fabric.microsoft.com/v1/operations/op-001/result", result.stdout)
        self.assertIn("FABRIC_LAKEHOUSE_ID = lh-001", result.stdout)

    def test_capacity_lookup_and_paginated_workspace_discovery(self):
        for case in ("lookup", "pagination"):
            with self.subTest(case=case):
                result = self.run_provisioning(case)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Provisioning Complete!", result.stdout)
                if case == "pagination":
                    self.assertNotIn("CALL: POST https://api.fabric.microsoft.com/v1/workspaces\n", result.stdout)

    def test_failures_never_report_completion(self):
        for case, error in (
            ("capacity-failure", "Capacity assignment failed"),
            ("operation-failure", "Item creation failed"),
            ("missing-id", "Workspace creation did not return an ID"),
            ("save-failure", "Failed to save FABRIC_WORKSPACE_ID"),
        ):
            with self.subTest(case=case):
                result = self.run_provisioning(case)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertNotIn("Provisioning Complete!", result.stdout)


class TestFabricDeploymentWiring(unittest.TestCase):
    def test_numeric_placeholders_are_rendered_and_fabric_remains_opt_in(self):
        root = Path(__file__).resolve().parents[1]
        hook = (root / "infra" / "hooks" / "postprovision.ps1").read_text(encoding="utf-8-sig")
        shell_hook = (root / "infra" / "hooks" / "postprovision.sh").read_text(encoding="utf-8")
        template = (root / "k8s" / "mcp-agents-deployment.yaml").read_text(encoding="utf-8")
        for name, default in (
            ("FABRIC_AGENT_MAX_RETRIES", "3"),
            ("FABRIC_PIPELINE_POLL_INTERVAL", "30"),
            ("FABRIC_PIPELINE_TIMEOUT", "3600"),
            ("ONELAKE_MAX_DOWNLOAD_BYTES", "52428800"),
        ):
            self.assertIn("${" + name + ":-" + default + "}", template)
            self.assertIn(r"\$\{" + name + ":-" + default + r"\}", hook)
            self.assertIn(r"\${" + name + ":-" + default + "}", shell_hook)
        self.assertIn("$fabricEnabled = 'false'", hook)
        self.assertRegex(template, r'name: FABRIC_DATA_AGENTS_ENABLED\s+value: "false"')
        self.assertNotIn('& "./scripts/deploy-fabric-workspace.ps1"', hook)
        dockerfile = (root / "src" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY *.py ./", dockerfile)

    def test_dotenv_is_loaded_before_fabric_module_configuration(self):
        root = Path(__file__).resolve().parents[1]
        module = ast.parse((root / "src" / "next_best_action_agent.py").read_text(encoding="utf-8"))
        load_line = next(
            node.lineno for node in module.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "load_dotenv"
        )
        imports = [
            node.lineno for node in ast.walk(module)
            if isinstance(node, ast.ImportFrom)
            and node.module in ("fabric_tools", "fabric_agents", "fabric_onelake")
        ]
        self.assertTrue(imports)
        self.assertLess(load_line, min(imports))


if __name__ == "__main__":
    unittest.main(verbosity=2)
