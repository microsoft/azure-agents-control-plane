"""Offline audit of the main agent's approval wiring, not a deployed E2E test.

The main module initializes Azure clients and loads local environment files at
import time. Compile only its actual handlers, result types and MCP schema, with
external services replaced by mocks. Do not import/start the full application.
The function bodies are unchanged; only the AI-function decorator is removed.

All enforcement assertions are required to pass; no expected-failure markers.
No real Teams, Azure, model, token acquisition, environment-file reads, or
deployment is involved. Both entry points use the same asynchronous checkpoint.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src import agent365_approval as approvals


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "src/next_best_action_agent.py"
WORKFLOW = ROOT / "agent365/workflows/agent_approval_logic_app.json"
LEGACY_TASK = "Set up a Agents pipeline for deploying microservices to Kubernetes"
DEPLOY_TASK = "Deploy the API to Kubernetes"
APPROVAL_ID = "33333333-3333-3333-3333-333333333333"
TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
APPROVER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONFIG = {
    "AZURE_TENANT_ID": TENANT,
    "APPROVAL_APPROVER_TENANT_ID": TENANT,
    "AZURE_CLIENT_ID": APPROVAL_ID,
    "DEPLOYMENT_ENVIRONMENT": "test",
    "AKS_CLUSTER_NAME": "offline-cluster",
    "K8S_NAMESPACE": "mcp-agents",
    "IMAGE_TAG": "offline-test",
    "APPROVAL_CALLBACK_AUDIENCE": f"api://{BLUEPRINT}",
    "APPROVAL_CALLBACK_PRINCIPAL_ID": APPROVAL_ID,
    "APPROVAL_APPROVER_IDS": APPROVER,
    "APPROVAL_TIMEOUT_HOURS": "2",
    "LOGIC_APP_APPROVAL_WEBHOOK": "https://workflow.example/triggers/manual/invoke?sig=fake-secret",
    "APPROVAL_CALLBACK_URL": "https://gateway.example/agent-approvals/callback",
}


def source_tree() -> ast.Module:
    return ast.parse(MAIN.read_text(encoding="utf-8"), filename=str(MAIN))


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in CONFIG.items():
        monkeypatch.setenv(name, value)
    for name in ("get_agent_credential", "CosmosClient"):
        monkeypatch.setattr(approvals, name, Mock(side_effect=AssertionError("Unexpected Azure access")))
    monkeypatch.setattr(
        approvals.aiohttp, "ClientSession", Mock(side_effect=AssertionError("Unexpected HTTP")),
    )


@pytest.fixture
def harness() -> Any:
    contract = approvals.ApprovalContract(
        approval_id=APPROVAL_ID, requested_by="offline-requester",
        task=LEGACY_TASK, environment="test",
        notification_status="sent", request_hash="0" * 64,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    )
    engine = SimpleNamespace(
        initiate_approval=AsyncMock(return_value=contract),
        resume_approval=AsyncMock(return_value=contract),
    )
    namespace = {
        "__name__": __name__, "app": FastAPI(), "Request": Request,
        "JSONResponse": JSONResponse, "dataclass": dataclass, "asdict": asdict,
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
        "json": json, "os": os, "uuid": uuid, "datetime": datetime, "timezone": timezone,
        "asyncio": asyncio, "time": time, "logger": Mock(),
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example/offline",
        "cosmos_tasks_container": Mock(), "cosmos_plans_container": Mock(),
        "get_embedding": Mock(return_value=[1.0, 0.0]),
        "analyze_intent": Mock(return_value="offline-analysis"),
        "find_similar_tasks": Mock(return_value=[]),
        "generate_plan_with_instructions": Mock(return_value=[{
            "step": 1, "action": "Offline recommendation", "description": "Nothing executed",
        }]),
        "long_term_memory": None, "facts_memory": None, "episode_capture": None,
        "AGENT365_APPROVAL_AVAILABLE": True,
        "ApprovalDecision": approvals.ApprovalDecision,
        "AgentValidationStatus": approvals.AgentValidationStatus,
        "ApprovalWorkflowEngine": approvals.ApprovalWorkflowEngine,
        "ApprovalError": approvals.ApprovalError,
        "ApprovalValidationError": approvals.ApprovalValidationError,
        "get_approval_workflow_engine": Mock(return_value=engine),
    }
    wanted = {
        "MCPTool", "MCPToolResult", "_normalize_plan_steps", "next_best_action_tool",
        "execute_tool", "_execute_tool_impl", "mcp_message_endpoint",
        "_next_best_action_approval",
    }
    nodes = []
    for original in source_tree().body:
        if isinstance(original, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and original.name in wanted:
            node = copy.deepcopy(original)
            if node.name == "next_best_action_tool":
                node.decorator_list = []
            nodes.append(node)
        elif isinstance(original, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TOOLS" for target in original.targets
        ):
            # Keep the exact published schema, not a test-authored substitute.
            node = copy.deepcopy(original)
            assert isinstance(node.value, ast.List)
            node.value.elts = [call for call in node.value.elts if isinstance(call, ast.Call) and any(
                kw.arg == "name" and isinstance(kw.value, ast.Constant) and kw.value.value == "next_best_action"
                for kw in call.keywords
            )]
            assert len(node.value.elts) == 1
            nodes.append(node)
    assert {node.name for node in nodes if hasattr(node, "name")} == wanted
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(MAIN), "exec", dont_inherit=True), namespace)
    with TestClient(namespace["app"]) as client:
        yield SimpleNamespace(ns=namespace, engine=engine, contract=contract, client=client)


def mcp_call(harness: Any, task: str, **arguments: Any) -> dict[str, Any]:
    response = harness.client.post("/runtime/webhooks/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "next_best_action", "arguments": {"task": task, **arguments}},
    })
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


def assert_no_planning(harness: Any, result: dict[str, Any]) -> None:
    assert "plan" not in result, "A recommendation was returned without a verified approval"
    for name in ("get_embedding", "analyze_intent", "generate_plan_with_instructions"):
        harness.ns[name].assert_not_called()
    for name in ("cosmos_tasks_container", "cosmos_plans_container"):
        harness.ns[name].upsert_item.assert_not_called()


def direct_call(harness: Any, task: str, approval_id: str | None = None) -> dict[str, Any]:
    return json.loads(asyncio.run(harness.ns["next_best_action_tool"](task, approval_id)))


def test_main_approval_imports_exist() -> None:
    imported = [alias.name for node in ast.walk(source_tree()) if isinstance(node, ast.ImportFrom)
                and node.module == "agent365_approval" for alias in node.names]
    assert imported, "Approval integration import is missing"
    missing = [name for name in imported if not hasattr(approvals, name)]
    assert not missing, f"Optional import fails for: {missing}"


@pytest.mark.parametrize("task", [LEGACY_TASK, DEPLOY_TASK], ids=["legacy-phrase", "deployment"])
def test_mcp_blocks_pending_deployment(harness: Any, task: str) -> None:
    result = mcp_call(harness, task)
    assert_no_planning(harness, result)
    assert result["status"] == "approval_pending"


def test_direct_tool_gates_other_deployment_wording(harness: Any) -> None:
    assert approvals.ApprovalWorkflowEngine.requires_approval(DEPLOY_TASK)
    result = direct_call(harness, DEPLOY_TASK)
    assert_no_planning(harness, result)


def test_missing_approval_module_fails_closed(harness: Any) -> None:
    harness.ns["AGENT365_APPROVAL_AVAILABLE"] = False
    result = direct_call(harness, LEGACY_TASK)
    assert_no_planning(harness, result)


def test_approval_exception_fails_closed(harness: Any) -> None:
    harness.engine.initiate_approval.side_effect = approvals.ApprovalConfigurationError("Offline unavailable configuration")
    result = direct_call(harness, LEGACY_TASK)
    assert_no_planning(harness, result)


@pytest.mark.parametrize(("decision", "validation"), [
    ("error", "failed"), ("timeout", "failed"), ("approved", "failed"), ("approved", "pending"),
], ids=["dispatch-error", "timeout", "validation-failed", "validation-pending"])
def test_only_approved_and_validated_can_plan(harness: Any, decision: str, validation: str) -> None:
    harness.contract.decision = decision
    harness.contract.agent_validation = validation
    result = direct_call(harness, LEGACY_TASK, APPROVAL_ID)
    assert_no_planning(harness, result)


@pytest.mark.parametrize("decision", ["pending", "rejected"])
def test_checkpoint_blocks_pending_and_rejected(harness: Any, decision: str) -> None:
    harness.contract.decision = decision
    result = direct_call(harness, LEGACY_TASK)
    assert_no_planning(harness, result)
    assert result["status"] == f"approval_{decision}"
    harness.engine.initiate_approval.assert_awaited_once()


def test_checkpoint_can_plan_only_on_approved_passed_resume(harness: Any) -> None:
    harness.contract.decision = "approved"
    harness.contract.agent_validation = "passed"
    result = direct_call(harness, LEGACY_TASK, APPROVAL_ID)
    assert result["plan"]["total_steps"] == 1
    harness.ns["generate_plan_with_instructions"].assert_called_once()
    harness.engine.resume_approval.assert_awaited_once()
    harness.engine.initiate_approval.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["mcp", "tool"])
def test_non_deployment_recommendation_is_not_in_the_deployment_policy(harness: Any, entrypoint: str) -> None:
    task = "Analyze customer churn"
    assert not approvals.ApprovalWorkflowEngine.requires_approval(task)
    result = mcp_call(harness, task) if entrypoint == "mcp" else direct_call(harness, task)
    assert result["plan"]["total_steps"] == 1
    harness.engine.initiate_approval.assert_not_awaited()


def test_mcp_resumes_existing_approval_before_planning(harness: Any) -> None:
    harness.contract.decision = "approved"
    harness.contract.agent_validation = "passed"
    mcp_call(harness, LEGACY_TASK, approval_id=APPROVAL_ID)
    harness.engine.resume_approval.assert_awaited_once()
    harness.engine.initiate_approval.assert_not_awaited()


def test_mcp_schema_advertises_approval_resume(harness: Any) -> None:
    response = harness.client.post("/runtime/webhooks/mcp/message", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    assert response.status_code == 200
    tool = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "next_best_action")
    assert "approval_id" in tool["inputSchema"]["properties"]


def test_main_mounts_callback_router() -> None:
    tree = source_tree()
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and node.module in ("approval_api", "src.approval_api")]
    aliases = {alias.asname or alias.name for node in imports for alias in node.names if alias.name == "router"}
    assert aliases, "Main app does not import the callback router"
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "include_router" and node.args
               and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases
               for node in ast.walk(tree)), "Main app does not mount the callback router"


@pytest.mark.parametrize("callback", [True, False], ids=["callback-configured", "callback-unset"])
def test_real_engine_payload_fits_workflow_schema(monkeypatch: pytest.MonkeyPatch, callback: bool) -> None:
    if not callback:
        monkeypatch.delenv("APPROVAL_CALLBACK_URL")
    transport = SimpleNamespace(send=AsyncMock())
    engine = approvals.ApprovalWorkflowEngine(
        transport=transport, clock=lambda: datetime(2026, 9, 6, 12, tzinfo=timezone.utc),
    )
    # Only persistence is stubbed: exercise the real payload construction.
    monkeypatch.setattr(engine, "_create", AsyncMock())
    monkeypatch.setattr(engine, "_record_notification", AsyncMock())
    asyncio.run(engine.initiate_approval(
        task=DEPLOY_TASK, requested_by="offline-requester", environment="test",
        cluster="offline-cluster", commit_sha="abc123",
    ))
    payload = transport.send.await_args.args[0]
    schema = json.loads(WORKFLOW.read_text(encoding="utf-8"))["triggers"]["When_an_HTTP_request_is_received"]["inputs"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= payload.keys()
    extra = set(payload) - schema["properties"].keys()
    assert not extra, f"Trigger schema rejects engine fields: {sorted(extra)}"


def test_parent_deployment_binds_current_approval_modules() -> None:
    parent = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
    child = (ROOT / "infra/app/agents-approval-logicapp.bicep").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^module agentsApprovalLogicApp '[^']+'[^\n]*\n.*?^\}", parent)
    assert match is not None
    supplied = set(re.findall(r"(?m)^    (\w+):", match.group(0)))
    declared = set(re.findall(r"(?m)^param (\w+) ", child))
    assert supplied <= declared, f"Parent supplies removed parameters: {sorted(supplied - declared)}"
    assert {"callbackUrl", "callbackAudience", "approverIds"} <= supplied
    assert "'./app/apim-approval-callback.bicep'" in parent


@pytest.mark.parametrize("extra", [
    {"callback_url": "https://attacker.example"}, {"approved": True},
    {"requested_by": "attacker"}, {"environment": "other"}, {"approval_id": ""},
    {"approval_id": False}, {"approval_id": "0" * 36},
], ids=["route", "approval-flag", "requester", "environment", "empty-id", "boolean-id", "invalid-id"])
def test_mcp_rejects_untrusted_context_or_bad_resume_id(harness: Any, extra: dict[str, Any]) -> None:
    result = mcp_call(harness, DEPLOY_TASK, **extra)
    assert_no_planning(harness, result)
    harness.engine.initiate_approval.assert_not_awaited()
    harness.engine.resume_approval.assert_not_awaited()


def test_changed_non_deployment_task_cannot_reuse_approval(harness: Any) -> None:
    harness.engine.resume_approval.side_effect = approvals.ApprovalConflictError("Approval request context does not match.")
    result = mcp_call(harness, "Summarize a document", approval_id=APPROVAL_ID)
    assert_no_planning(harness, result)
    harness.engine.resume_approval.assert_awaited_once()


def test_expired_approval_is_blocked_even_if_mock_engine_returns_approved(harness: Any) -> None:
    harness.contract.decision, harness.contract.agent_validation = "approved", "passed"
    harness.contract.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert_no_planning(harness, mcp_call(harness, LEGACY_TASK, approval_id=APPROVAL_ID))


def test_unexpected_approval_error_is_secret_safe(harness: Any) -> None:
    harness.engine.initiate_approval.side_effect = RuntimeError("secret-provider-url")
    result = mcp_call(harness, DEPLOY_TASK)
    assert_no_planning(harness, result)
    assert "secret-provider-url" not in json.dumps(result)