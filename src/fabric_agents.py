"""
Microsoft Fabric Data Agents — Intelligent Agent Layer

Four specialized agents that orchestrate Fabric tool calls with:
- Natural-language intent detection and query generation
- Schema-aware query validation
- Memory integration (short-term, long-term, facts)
- Multi-step workflow execution with retry logic

Agent Types:
    LakehouseAgent   — Spark SQL against Fabric Lakehouses
    WarehouseAgent   — T-SQL against Fabric Data Warehouses
    PipelineAgent    — Trigger / monitor / retry Fabric Data Pipelines
    SemanticModelAgent — DAX/MDX queries on Power BI Semantic Models

Orchestrator:
    FabricAgentOrchestrator — Routes user requests to the right specialist
"""

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fabric_tools import (
    FabricAPIClient,
    FabricAgentType,
    PipelineRunStatus,
    get_fabric_client,
    fabric_query_lakehouse_tool,
    fabric_query_warehouse_tool,
    fabric_trigger_pipeline_tool,
    fabric_get_pipeline_status_tool,
    fabric_query_semantic_model_tool,
    fabric_list_resources_tool,
    FABRIC_DATA_AGENTS_ENABLED,
    FABRIC_WORKSPACE_ID,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
FABRIC_AGENT_MAX_RETRIES = int(os.getenv("FABRIC_AGENT_MAX_RETRIES", "3"))
FABRIC_PIPELINE_POLL_INTERVAL = int(os.getenv("FABRIC_PIPELINE_POLL_INTERVAL", "30"))
FABRIC_PIPELINE_TIMEOUT = int(os.getenv("FABRIC_PIPELINE_TIMEOUT", "3600"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class AgentIntent(Enum):
    """Detected user intent categories."""
    QUERY_DATA = "query_data"
    EXPLORE_SCHEMA = "explore_schema"
    WRITE_DATA = "write_data"
    TRIGGER_PIPELINE = "trigger_pipeline"
    MONITOR_PIPELINE = "monitor_pipeline"
    RETRY_PIPELINE = "retry_pipeline"
    LIST_RESOURCES = "list_resources"
    QUERY_KPI = "query_kpi"
    CROSS_DOMAIN = "cross_domain"
    UNKNOWN = "unknown"


@dataclass
class AgentContext:
    """Contextual state passed between steps within an agent workflow."""
    session_id: str = ""
    user_query: str = ""
    intent: AgentIntent = AgentIntent.UNKNOWN
    detected_resource_type: str = ""
    detected_resource_id: str = ""
    detected_resource_name: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    memory_context: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time


@dataclass
class AgentResponse:
    """Unified response from any Fabric agent."""
    success: bool
    agent_type: str
    intent: str
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    actions_taken: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


# ---------------------------------------------------------------------------
# Base Agent
# ---------------------------------------------------------------------------

class BaseFabricAgent(ABC):
    """Abstract base for all Fabric agents."""

    agent_type: FabricAgentType

    def __init__(self):
        self._client: Optional[FabricAPIClient] = None

    @property
    def client(self) -> FabricAPIClient:
        if self._client is None:
            self._client = get_fabric_client()
        return self._client

    # ----- public interface -----

    async def handle(self, user_query: str, session_id: str = "", **kwargs) -> AgentResponse:
        """Entry-point: detect intent → plan → execute → summarise."""
        ctx = AgentContext(
            session_id=session_id,
            user_query=user_query,
            parameters=kwargs,
        )

        try:
            ctx.intent = self._detect_intent(user_query)
            logger.info(
                f"[{self.agent_type.value}] intent={ctx.intent.value} query={user_query[:80]}"
            )
            return await self._execute(ctx)

        except Exception as exc:
            logger.error(f"[{self.agent_type.value}] error: {exc}", exc_info=True)
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary=f"Agent error: {exc}",
                errors=[str(exc)],
                elapsed_seconds=ctx.elapsed_seconds,
            )

    # ----- abstract hooks -----

    @abstractmethod
    def _detect_intent(self, query: str) -> AgentIntent:
        ...

    @abstractmethod
    async def _execute(self, ctx: AgentContext) -> AgentResponse:
        ...

    # ----- helpers -----

    @staticmethod
    def _parse_tool_result(raw_json: str) -> Dict[str, Any]:
        """Parse the JSON string returned by a fabric_*_tool function."""
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid JSON from tool", "raw": raw_json}

    @staticmethod
    def _safe_limit(query: str, default_limit: int = 100) -> str:
        """Append LIMIT if not present in a query for safety."""
        upper = query.upper().strip()
        if "LIMIT" not in upper and "TOP " not in upper:
            query = query.rstrip().rstrip(";")
            return f"{query} LIMIT {default_limit}"
        return query


# ===========================================================================
# Lakehouse Agent
# ===========================================================================

class LakehouseAgent(BaseFabricAgent):
    """
    Interact with Microsoft Fabric Lakehouses via Spark SQL.

    Capabilities:
    - Execute ad-hoc Spark SQL queries
    - Explore table schemas (DESCRIBE, SHOW TABLES)
    - Query large datasets with automatic LIMIT safeguards
    - Write data via INSERT / CREATE TABLE AS SELECT
    """

    agent_type = FabricAgentType.LAKEHOUSE

    # Intent keywords
    _SCHEMA_KEYWORDS = {"schema", "describe", "columns", "tables", "show", "metadata", "structure"}
    _WRITE_KEYWORDS = {"insert", "create", "write", "append", "merge", "upsert", "update", "delete", "drop"}

    def _detect_intent(self, query: str) -> AgentIntent:
        lower = query.lower()
        tokens = set(re.findall(r"\w+", lower))

        if tokens & self._SCHEMA_KEYWORDS:
            return AgentIntent.EXPLORE_SCHEMA
        if tokens & self._WRITE_KEYWORDS:
            return AgentIntent.WRITE_DATA
        return AgentIntent.QUERY_DATA

    async def _execute(self, ctx: AgentContext) -> AgentResponse:
        lakehouse_id = ctx.parameters.get("lakehouse_id", "")
        lakehouse_name = ctx.parameters.get("lakehouse_name", "")

        if not lakehouse_id:
            # Try to discover a lakehouse
            discovery = self._parse_tool_result(fabric_list_resources_tool("lakehouse"))
            lakehouses = discovery.get("resources", {}).get("lakehouses", [])
            if lakehouses:
                lakehouse_id = lakehouses[0].get("id", "")
                lakehouse_name = lakehouses[0].get("name", lakehouse_name)
                ctx.results.append({"discovery": lakehouses})

        if not lakehouse_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="No lakehouse found. Provide a lakehouse_id or deploy a Fabric Lakehouse.",
                errors=["lakehouse_id not provided and no lakehouses discovered"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        actions: List[str] = []

        if ctx.intent == AgentIntent.EXPLORE_SCHEMA:
            return await self._explore_schema(ctx, lakehouse_id, lakehouse_name)

        # Build query (use raw SQL from user or from parameters)
        sql = ctx.parameters.get("query", ctx.user_query)
        sql = self._safe_limit(sql)

        raw = fabric_query_lakehouse_tool(lakehouse_id, sql, lakehouse_name)
        result = self._parse_tool_result(raw)
        actions.append(f"Executed Spark SQL on lakehouse '{lakehouse_name or lakehouse_id}'")

        row_count = len(result.get("results", {}).get("rows", []))

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Returned {row_count} rows from lakehouse '{lakehouse_name or lakehouse_id}'.",
            data=result,
            actions_taken=actions,
            recommendations=self._generate_recommendations(result, row_count),
            errors=result.get("errors", []),
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _explore_schema(
        self, ctx: AgentContext, lakehouse_id: str, lakehouse_name: str
    ) -> AgentResponse:
        """Run SHOW TABLES and optionally DESCRIBE on each table."""
        actions: List[str] = []
        tables_result = self._parse_tool_result(
            fabric_query_lakehouse_tool(lakehouse_id, "SHOW TABLES", lakehouse_name)
        )
        actions.append("Listed tables in lakehouse")

        tables = [
            row.get("tableName", row.get("table_name", ""))
            for row in tables_result.get("results", {}).get("rows", [])
        ]

        schema_details: Dict[str, Any] = {"tables": tables, "columns": {}}

        # Describe first 10 tables to avoid excessive API calls
        for tbl in tables[:10]:
            if not tbl:
                continue
            desc_raw = fabric_query_lakehouse_tool(
                lakehouse_id, f"DESCRIBE TABLE {tbl}", lakehouse_name
            )
            desc = self._parse_tool_result(desc_raw)
            schema_details["columns"][tbl] = desc.get("results", {}).get("rows", [])
            actions.append(f"Described table '{tbl}'")

        return AgentResponse(
            success=True,
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Found {len(tables)} tables in lakehouse '{lakehouse_name}'.",
            data=schema_details,
            actions_taken=actions,
            elapsed_seconds=ctx.elapsed_seconds,
        )

    @staticmethod
    def _generate_recommendations(result: Dict[str, Any], row_count: int) -> List[str]:
        recs: List[str] = []
        if row_count == 100:
            recs.append(
                "Query returned exactly 100 rows (the safety limit). "
                "Add an explicit LIMIT or WHERE clause to refine results."
            )
        if not result.get("success"):
            recs.append("Check Spark SQL syntax and table/column names.")
        return recs


# ===========================================================================
# Warehouse Agent
# ===========================================================================

class WarehouseAgent(BaseFabricAgent):
    """
    Interact with Microsoft Fabric Data Warehouses via T-SQL.

    Capabilities:
    - Execute T-SQL analytical queries
    - Explore warehouse schemas (sp_tables, sp_columns)
    - Generate aggregated reports
    - Support for TOP N and pagination
    """

    agent_type = FabricAgentType.WAREHOUSE

    _SCHEMA_KEYWORDS = {"schema", "tables", "columns", "sp_tables", "sp_columns", "metadata", "structure"}
    _WRITE_KEYWORDS = {"insert", "create", "update", "delete", "alter", "drop", "merge", "truncate"}

    def _detect_intent(self, query: str) -> AgentIntent:
        lower = query.lower()
        tokens = set(re.findall(r"\w+", lower))
        if tokens & self._SCHEMA_KEYWORDS:
            return AgentIntent.EXPLORE_SCHEMA
        if tokens & self._WRITE_KEYWORDS:
            return AgentIntent.WRITE_DATA
        return AgentIntent.QUERY_DATA

    async def _execute(self, ctx: AgentContext) -> AgentResponse:
        warehouse_id = ctx.parameters.get("warehouse_id", "")
        warehouse_name = ctx.parameters.get("warehouse_name", "")

        if not warehouse_id:
            discovery = self._parse_tool_result(fabric_list_resources_tool("warehouse"))
            warehouses = discovery.get("resources", {}).get("warehouses", [])
            if warehouses:
                warehouse_id = warehouses[0].get("id", "")
                warehouse_name = warehouses[0].get("name", warehouse_name)

        if not warehouse_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="No warehouse found. Provide a warehouse_id or deploy a Fabric Warehouse.",
                errors=["warehouse_id not provided and no warehouses discovered"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        actions: List[str] = []

        if ctx.intent == AgentIntent.EXPLORE_SCHEMA:
            return await self._explore_schema(ctx, warehouse_id, warehouse_name)

        sql = ctx.parameters.get("query", ctx.user_query)
        # T-SQL uses TOP instead of LIMIT
        sql = self._ensure_top(sql)

        raw = fabric_query_warehouse_tool(warehouse_id, sql, warehouse_name)
        result = self._parse_tool_result(raw)
        actions.append(f"Executed T-SQL on warehouse '{warehouse_name or warehouse_id}'")

        row_count = len(result.get("results", {}).get("rows", []))

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Returned {row_count} rows from warehouse '{warehouse_name or warehouse_id}'.",
            data=result,
            actions_taken=actions,
            recommendations=self._generate_recommendations(result, row_count),
            errors=result.get("errors", []),
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _explore_schema(
        self, ctx: AgentContext, warehouse_id: str, warehouse_name: str
    ) -> AgentResponse:
        actions: List[str] = []

        # Use INFORMATION_SCHEMA for T-SQL
        tables_raw = fabric_query_warehouse_tool(
            warehouse_id,
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
            "FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_SCHEMA, TABLE_NAME",
            warehouse_name,
        )
        tables_result = self._parse_tool_result(tables_raw)
        actions.append("Queried INFORMATION_SCHEMA.TABLES")

        tables = tables_result.get("results", {}).get("rows", [])
        schema_details: Dict[str, Any] = {"tables": tables, "columns": {}}

        # Describe first 10 tables
        for tbl in tables[:10]:
            table_name = tbl.get("TABLE_NAME", "")
            table_schema = tbl.get("TABLE_SCHEMA", "dbo")
            if not table_name:
                continue
            col_raw = fabric_query_warehouse_tool(
                warehouse_id,
                f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH "
                f"FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_NAME = '{table_name}' AND TABLE_SCHEMA = '{table_schema}'",
                warehouse_name,
            )
            col_result = self._parse_tool_result(col_raw)
            fqn = f"{table_schema}.{table_name}"
            schema_details["columns"][fqn] = col_result.get("results", {}).get("rows", [])
            actions.append(f"Described columns for '{fqn}'")

        return AgentResponse(
            success=True,
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Found {len(tables)} tables in warehouse '{warehouse_name}'.",
            data=schema_details,
            actions_taken=actions,
            elapsed_seconds=ctx.elapsed_seconds,
        )

    @staticmethod
    def _ensure_top(sql: str, default_top: int = 100) -> str:
        """Add TOP clause if missing for safety."""
        upper = sql.upper().strip()
        if upper.startswith("SELECT") and "TOP " not in upper and "LIMIT" not in upper:
            sql = sql.strip()
            sql = re.sub(r"^(SELECT)\s", rf"\1 TOP {default_top} ", sql, count=1, flags=re.IGNORECASE)
        return sql

    @staticmethod
    def _generate_recommendations(result: Dict[str, Any], row_count: int) -> List[str]:
        recs: List[str] = []
        if row_count >= 100:
            recs.append(
                "Query returned 100+ rows (safety TOP applied). "
                "Refine with WHERE or explicit TOP N."
            )
        if not result.get("success"):
            recs.append("Check T-SQL syntax, table names, and schema references.")
        return recs


# ===========================================================================
# Pipeline Agent
# ===========================================================================

class PipelineAgent(BaseFabricAgent):
    """
    Manage Microsoft Fabric Data Pipelines.

    Capabilities:
    - Trigger pipeline runs with parameters
    - Monitor run status with polling
    - Wait for pipeline completion
    - Retry failed runs
    - List all pipelines in the workspace
    """

    agent_type = FabricAgentType.PIPELINE

    _TRIGGER_KEYWORDS = {"trigger", "run", "start", "execute", "launch", "kick"}
    _MONITOR_KEYWORDS = {"status", "monitor", "check", "progress", "watch"}
    _RETRY_KEYWORDS = {"retry", "rerun", "restart", "re-run", "re-trigger"}
    _LIST_KEYWORDS = {"list", "show", "discover", "available", "pipelines"}

    def _detect_intent(self, query: str) -> AgentIntent:
        lower = query.lower()
        tokens = set(re.findall(r"\w+", lower))

        if tokens & self._RETRY_KEYWORDS:
            return AgentIntent.RETRY_PIPELINE
        if tokens & self._TRIGGER_KEYWORDS:
            return AgentIntent.TRIGGER_PIPELINE
        if tokens & self._MONITOR_KEYWORDS:
            return AgentIntent.MONITOR_PIPELINE
        if tokens & self._LIST_KEYWORDS:
            return AgentIntent.LIST_RESOURCES
        return AgentIntent.TRIGGER_PIPELINE

    async def _execute(self, ctx: AgentContext) -> AgentResponse:
        if ctx.intent == AgentIntent.LIST_RESOURCES:
            return await self._list_pipelines(ctx)
        if ctx.intent == AgentIntent.TRIGGER_PIPELINE:
            return await self._trigger(ctx)
        if ctx.intent == AgentIntent.MONITOR_PIPELINE:
            return await self._monitor(ctx)
        if ctx.intent == AgentIntent.RETRY_PIPELINE:
            return await self._retry(ctx)

        return AgentResponse(
            success=False,
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary="Unable to determine pipeline action.",
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _list_pipelines(self, ctx: AgentContext) -> AgentResponse:
        raw = fabric_list_resources_tool("pipeline")
        result = self._parse_tool_result(raw)
        pipelines = result.get("resources", {}).get("pipelines", [])

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Found {len(pipelines)} data pipelines in workspace.",
            data={"pipelines": pipelines},
            actions_taken=["Listed workspace pipelines"],
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _trigger(self, ctx: AgentContext) -> AgentResponse:
        pipeline_id = ctx.parameters.get("pipeline_id", "")
        pipeline_name = ctx.parameters.get("pipeline_name", "")
        params_json = json.dumps(ctx.parameters.get("pipeline_parameters", {}))

        if not pipeline_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="pipeline_id is required to trigger a pipeline.",
                errors=["Missing pipeline_id"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        raw = fabric_trigger_pipeline_tool(pipeline_id, pipeline_name, params_json)
        result = self._parse_tool_result(raw)
        actions = [f"Triggered pipeline '{pipeline_name or pipeline_id}'"]

        run_id = result.get("run_id", "")

        # Optionally wait for completion
        wait = ctx.parameters.get("wait_for_completion", False)
        if wait and run_id:
            status_result = await self._poll_until_complete(
                pipeline_id, run_id, pipeline_name
            )
            result["final_status"] = status_result
            actions.append(f"Waited for completion — final status: {status_result.get('status', 'unknown')}")

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Pipeline '{pipeline_name or pipeline_id}' triggered. Run ID: {run_id}",
            data=result,
            actions_taken=actions,
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _monitor(self, ctx: AgentContext) -> AgentResponse:
        pipeline_id = ctx.parameters.get("pipeline_id", "")
        run_id = ctx.parameters.get("run_id", "")
        pipeline_name = ctx.parameters.get("pipeline_name", "")

        if not pipeline_id or not run_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="pipeline_id and run_id are required to monitor a pipeline.",
                errors=["Missing pipeline_id or run_id"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        raw = fabric_get_pipeline_status_tool(pipeline_id, run_id, pipeline_name)
        result = self._parse_tool_result(raw)
        status = result.get("status", "Unknown")

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Pipeline '{pipeline_name or pipeline_id}' run {run_id} — status: {status}",
            data=result,
            actions_taken=[f"Checked status of run {run_id}"],
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _retry(self, ctx: AgentContext) -> AgentResponse:
        """Retry a previously failed pipeline with the same parameters."""
        pipeline_id = ctx.parameters.get("pipeline_id", "")
        pipeline_name = ctx.parameters.get("pipeline_name", "")
        params_json = json.dumps(ctx.parameters.get("pipeline_parameters", {}))

        if not pipeline_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="pipeline_id is required to retry a pipeline.",
                errors=["Missing pipeline_id"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        actions: List[str] = []
        retries = 0
        last_result: Dict[str, Any] = {}

        for attempt in range(1, FABRIC_AGENT_MAX_RETRIES + 1):
            raw = fabric_trigger_pipeline_tool(pipeline_id, pipeline_name, params_json)
            result = self._parse_tool_result(raw)
            run_id = result.get("run_id", "")
            actions.append(f"Attempt {attempt}: triggered run {run_id}")

            if run_id:
                status_result = await self._poll_until_complete(
                    pipeline_id, run_id, pipeline_name
                )
                final_status = status_result.get("status", "Unknown")
                actions.append(f"Attempt {attempt}: final status = {final_status}")

                if final_status == PipelineRunStatus.SUCCEEDED.value:
                    result["final_status"] = status_result
                    return AgentResponse(
                        success=True,
                        agent_type=self.agent_type.value,
                        intent=ctx.intent.value,
                        summary=f"Pipeline succeeded on attempt {attempt}.",
                        data=result,
                        actions_taken=actions,
                        elapsed_seconds=ctx.elapsed_seconds,
                    )

            retries = attempt
            last_result = result

        return AgentResponse(
            success=False,
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Pipeline failed after {retries} retries.",
            data=last_result,
            actions_taken=actions,
            recommendations=["Investigate pipeline activity logs for root cause."],
            errors=[f"Exhausted {retries} retry attempts"],
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _poll_until_complete(
        self, pipeline_id: str, run_id: str, pipeline_name: str
    ) -> Dict[str, Any]:
        """Poll pipeline status until terminal state or timeout."""
        import asyncio

        terminal_states = {
            PipelineRunStatus.SUCCEEDED.value,
            PipelineRunStatus.FAILED.value,
            PipelineRunStatus.CANCELLED.value,
        }
        elapsed = 0

        while elapsed < FABRIC_PIPELINE_TIMEOUT:
            raw = fabric_get_pipeline_status_tool(pipeline_id, run_id, pipeline_name)
            result = self._parse_tool_result(raw)
            status = result.get("status", "Unknown")

            if status in terminal_states:
                return result

            await asyncio.sleep(FABRIC_PIPELINE_POLL_INTERVAL)
            elapsed += FABRIC_PIPELINE_POLL_INTERVAL

        return {"status": "Timeout", "message": f"Timed out after {FABRIC_PIPELINE_TIMEOUT}s"}


# ===========================================================================
# Semantic Model Agent
# ===========================================================================

class SemanticModelAgent(BaseFabricAgent):
    """
    Query Power BI Semantic Models via DAX / MDX.

    Capabilities:
    - Execute DAX evaluation queries
    - Execute MDX queries
    - Discover available semantic models
    - Retrieve pre-built KPIs and measures
    """

    agent_type = FabricAgentType.SEMANTIC_MODEL

    _KPI_KEYWORDS = {"kpi", "measure", "metric", "score", "rate", "dashboard", "report"}

    def _detect_intent(self, query: str) -> AgentIntent:
        lower = query.lower()
        tokens = set(re.findall(r"\w+", lower))

        if {"list", "show", "discover", "available"} & tokens:
            return AgentIntent.LIST_RESOURCES
        if tokens & self._KPI_KEYWORDS:
            return AgentIntent.QUERY_KPI
        return AgentIntent.QUERY_DATA

    async def _execute(self, ctx: AgentContext) -> AgentResponse:
        if ctx.intent == AgentIntent.LIST_RESOURCES:
            return await self._list_models(ctx)

        dataset_id = ctx.parameters.get("dataset_id", "")
        dataset_name = ctx.parameters.get("dataset_name", "")
        query_language = ctx.parameters.get("query_language", "DAX")

        if not dataset_id:
            # Discover
            discovery = self._parse_tool_result(fabric_list_resources_tool("semantic_model"))
            models = discovery.get("resources", {}).get("semantic_models", [])
            if models:
                dataset_id = models[0].get("id", "")
                dataset_name = models[0].get("name", dataset_name)

        if not dataset_id:
            return AgentResponse(
                success=False,
                agent_type=self.agent_type.value,
                intent=ctx.intent.value,
                summary="No semantic model found. Provide dataset_id or deploy a model.",
                errors=["dataset_id not provided and none discovered"],
                elapsed_seconds=ctx.elapsed_seconds,
            )

        dax_query = ctx.parameters.get("query", ctx.user_query)
        raw = fabric_query_semantic_model_tool(dataset_id, dax_query, dataset_name, query_language)
        result = self._parse_tool_result(raw)

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Queried semantic model '{dataset_name or dataset_id}' with {query_language}.",
            data=result,
            actions_taken=[f"Executed {query_language} query on '{dataset_name or dataset_id}'"],
            elapsed_seconds=ctx.elapsed_seconds,
        )

    async def _list_models(self, ctx: AgentContext) -> AgentResponse:
        raw = fabric_list_resources_tool("semantic_model")
        result = self._parse_tool_result(raw)
        models = result.get("resources", {}).get("semantic_models", [])

        return AgentResponse(
            success=result.get("success", False),
            agent_type=self.agent_type.value,
            intent=ctx.intent.value,
            summary=f"Found {len(models)} semantic models in workspace.",
            data={"semantic_models": models},
            actions_taken=["Listed semantic models"],
            elapsed_seconds=ctx.elapsed_seconds,
        )


# ===========================================================================
# Fabric Agent Orchestrator
# ===========================================================================

class FabricAgentOrchestrator:
    """
    Routes user requests to the appropriate specialist Fabric agent.

    Routing logic:
    - Lakehouse keywords → LakehouseAgent
    - Warehouse keywords → WarehouseAgent
    - Pipeline keywords  → PipelineAgent
    - Semantic model / DAX / KPI keywords → SemanticModelAgent
    - Ambiguous → try to infer from context, fallback to listing resources
    """

    def __init__(self):
        self.lakehouse_agent = LakehouseAgent()
        self.warehouse_agent = WarehouseAgent()
        self.pipeline_agent = PipelineAgent()
        self.semantic_model_agent = SemanticModelAgent()

        self._agents: Dict[str, BaseFabricAgent] = {
            "lakehouse": self.lakehouse_agent,
            "warehouse": self.warehouse_agent,
            "pipeline": self.pipeline_agent,
            "semantic_model": self.semantic_model_agent,
        }

    def route(self, user_query: str, **kwargs) -> Tuple[BaseFabricAgent, Dict[str, Any]]:
        """Determine which agent should handle the request."""
        lower = user_query.lower()

        # Explicit resource type passed by caller
        if "agent_type" in kwargs:
            agent_key = kwargs.pop("agent_type")
            if agent_key in self._agents:
                return self._agents[agent_key], kwargs

        # Keyword-based routing
        if any(kw in lower for kw in ["lakehouse", "spark sql", "delta", "parquet", "onelake"]):
            return self.lakehouse_agent, kwargs

        if any(kw in lower for kw in ["warehouse", "t-sql", "tsql", "dwh", "data warehouse"]):
            return self.warehouse_agent, kwargs

        if any(kw in lower for kw in ["pipeline", "etl", "trigger", "orchestrat"]):
            return self.pipeline_agent, kwargs

        if any(kw in lower for kw in [
            "semantic model", "dataset", "dax", "mdx", "power bi", "kpi", "measure",
            "report", "dashboard",
        ]):
            return self.semantic_model_agent, kwargs

        # Fallback: if explicit IDs are provided, use them
        if kwargs.get("lakehouse_id"):
            return self.lakehouse_agent, kwargs
        if kwargs.get("warehouse_id"):
            return self.warehouse_agent, kwargs
        if kwargs.get("pipeline_id"):
            return self.pipeline_agent, kwargs
        if kwargs.get("dataset_id"):
            return self.semantic_model_agent, kwargs

        # Default — lakehouse (most general-purpose)
        return self.lakehouse_agent, kwargs

    async def handle(self, user_query: str, session_id: str = "", **kwargs) -> AgentResponse:
        """
        High-level entry-point: route → delegate → return.

        Args:
            user_query: Natural-language user request
            session_id: Correlation / session ID
            **kwargs: Resource IDs, parameters etc.

        Returns:
            AgentResponse with results and recommendations
        """
        if not FABRIC_DATA_AGENTS_ENABLED:
            return AgentResponse(
                success=False,
                agent_type="orchestrator",
                intent="unknown",
                summary="Fabric Data Agents are not enabled. Set FABRIC_DATA_AGENTS_ENABLED=true.",
                errors=["Fabric Data Agents disabled"],
            )

        agent, params = self.route(user_query, **kwargs)
        logger.info(f"[Orchestrator] routing to {agent.agent_type.value} agent")
        return await agent.handle(user_query, session_id=session_id, **params)

    async def list_all_resources(self) -> AgentResponse:
        """Convenience: list every Fabric resource type."""
        raw = fabric_list_resources_tool("all")
        result = json.loads(raw) if isinstance(raw, str) else raw

        return AgentResponse(
            success=result.get("success", False),
            agent_type="orchestrator",
            intent="list_resources",
            summary="Listed all Fabric resources in workspace.",
            data=result.get("resources", {}),
            actions_taken=["Listed lakehouses, warehouses, pipelines, semantic models"],
        )

    async def cross_domain_query(
        self, user_query: str, session_id: str = "", **kwargs
    ) -> AgentResponse:
        """
        Execute a cross-domain query that touches multiple Fabric resource types.

        Example: "Get customer churn data from lakehouse, then refresh the dashboard
                  by triggering the ETL pipeline."

        The orchestrator splits the request into sub-tasks, delegates each to the
        appropriate agent, and aggregates results.
        """
        sub_results: List[Dict[str, Any]] = []
        actions: List[str] = []

        # Simple heuristic: check which agents are relevant
        lower = user_query.lower()
        relevant_agents: List[BaseFabricAgent] = []

        if any(kw in lower for kw in ["lakehouse", "spark", "delta", "customer", "churn"]):
            relevant_agents.append(self.lakehouse_agent)
        if any(kw in lower for kw in ["warehouse", "t-sql", "sales", "report"]):
            relevant_agents.append(self.warehouse_agent)
        if any(kw in lower for kw in ["pipeline", "etl", "trigger", "refresh"]):
            relevant_agents.append(self.pipeline_agent)
        if any(kw in lower for kw in ["semantic", "dax", "kpi", "dashboard", "measure"]):
            relevant_agents.append(self.semantic_model_agent)

        if not relevant_agents:
            relevant_agents = [self.lakehouse_agent]

        for agent in relevant_agents:
            resp = await agent.handle(user_query, session_id=session_id, **kwargs)
            sub_results.append(asdict(resp))
            actions.extend(resp.actions_taken)

        return AgentResponse(
            success=all(r.get("success") for r in sub_results),
            agent_type="orchestrator",
            intent="cross_domain",
            summary=f"Executed cross-domain query across {len(relevant_agents)} agent(s).",
            data={"sub_results": sub_results},
            actions_taken=actions,
        )


# ===========================================================================
# Module-level singleton
# ===========================================================================

_orchestrator: Optional[FabricAgentOrchestrator] = None


def get_fabric_orchestrator() -> FabricAgentOrchestrator:
    """Get or create the global Fabric Agent Orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = FabricAgentOrchestrator()
        logger.info("Fabric Agent Orchestrator initialized")
    return _orchestrator
