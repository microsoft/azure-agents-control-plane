"""Offline approval tests: real engine, fake Cosmos CAS, fake clock and transport.

No Azure, Graph, credential acquisition, real HTTP, or command execution. These
tests use unittest's async support and can also be collected by pytest.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import threading
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from azure.core import MatchConditions
from azure.cosmos import exceptions as cosmos_exceptions

from src import agent365_approval as approvals


TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
PRINCIPAL = "33333333-3333-3333-3333-333333333333"
APPROVER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SECOND_APPROVER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OUTSIDER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
WEBHOOK = "https://workflow.example/workflows/approval/triggers/manual/paths/invoke?api-version=2016-10-01&sig=fake-secret"
CALLBACK = "https://gateway.example/mcp/approvals/callback"
CONFIG = {
    "AZURE_TENANT_ID": TENANT,
    "APPROVAL_CALLBACK_AUDIENCE": f"api://{BLUEPRINT}",
    "APPROVAL_CALLBACK_PRINCIPAL_ID": PRINCIPAL,
    "APPROVAL_APPROVER_IDS": f"{SECOND_APPROVER}, {APPROVER.upper()}, {APPROVER}",
    "APPROVAL_TIMEOUT_HOURS": "2",
    "LOGIC_APP_APPROVAL_WEBHOOK": WEBHOOK,
    "APPROVAL_CALLBACK_URL": CALLBACK,
    "COSMOSDB_ENDPOINT": "https://cosmos.example/",
    "COSMOSDB_DATABASE_NAME": "mcpdb",
    "COSMOSDB_APPROVALS_CONTAINER": "approvals",
}
CONTEXT = {
    "task": "Deploy the API to Kubernetes using the CI/CD pipeline",
    "requested_by": "requester-object-id",
    "environment": "production",
    "cluster": "cluster-a",
    "namespace": "api",
    "image_tags": ["registry.example/api:1", "registry.example/sidecar:2"],
    "commit_sha": "abc123",
    "pipeline_url": "https://pipeline.example/runs/123",
    "rollback_url": "https://pipeline.example/runs/rollback",
}


class CasContainer:
    """Thread-safe, partition-aware fake that enforces real Cosmos ETag semantics."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.reads: list[tuple[str, str]] = []
        self.replacements: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.sequence = 0
        self.fail_create: Exception | None = None
        self.fail_read: Exception | None = None
        self.fail_replace: Exception | None = None
        self.fail_after_replace: Exception | None = None
        self.conflicts = 0
        self.barrier: threading.Barrier | None = None
        self.barrier_reads = 0

    def _save(self, body: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        doc = copy.deepcopy(body)
        doc["_etag"] = f'"{self.sequence}"'
        self.docs[(doc["environment"], doc["id"])] = doc
        return copy.deepcopy(doc)

    def create_item(self, *, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.fail_create:
                raise self.fail_create
            if (body["environment"], body["id"]) in self.docs:
                raise cosmos_exceptions.CosmosHttpResponseError(status_code=409, message="Conflict")
            assert body["partitionKey"] == body["environment"]
            assert body["id"] == body["approval_id"]
            return self._save(body)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        with self.lock:
            self.reads.append((item, partition_key))
            if self.fail_read:
                raise self.fail_read
            doc = self.docs.get((partition_key, item))
            if doc is None:
                raise cosmos_exceptions.CosmosResourceNotFoundError(status_code=404, message="Not found")
            result = copy.deepcopy(doc)
            barrier = self.barrier if self.barrier_reads > 0 else None
            if barrier is not None:
                self.barrier_reads -= 1
        if barrier is not None:
            barrier.wait(timeout=5)
        return result

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: MatchConditions,
    ) -> dict[str, Any]:
        with self.lock:
            assert match_condition == MatchConditions.IfNotModified
            assert "_etag" not in body
            assert body["partitionKey"] == body["environment"]
            assert body["id"] == item
            if self.fail_replace:
                raise self.fail_replace
            current = self.docs[(body["environment"], item)]
            if self.conflicts > 0:
                self.conflicts -= 1
                raise cosmos_exceptions.CosmosHttpResponseError(status_code=412, message="Stale ETag")
            if current["_etag"] != etag:
                raise cosmos_exceptions.CosmosHttpResponseError(status_code=412, message="Stale ETag")
            self.replacements.append(copy.deepcopy(body))
            result = self._save(body)
            if self.fail_after_replace:
                error, self.fail_after_replace = self.fail_after_replace, None
                raise error
            return result

    def race_next_two_reads(self) -> None:
        self.barrier = threading.Barrier(2)
        self.barrier_reads = 2


class ApprovalEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, CONFIG)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("get_agent_credential", "CosmosClient"):
            guard = patch.object(approvals, name, side_effect=AssertionError("Unexpected Azure access"))
            guard.start()
            self.addCleanup(guard.stop)
        http_guard = patch.object(approvals.aiohttp, "ClientSession", side_effect=AssertionError("Unexpected HTTP"))
        http_guard.start()
        self.addCleanup(http_guard.stop)
        self.now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        self.container = CasContainer()
        self.transport = SimpleNamespace(send=AsyncMock())
        self.engine = self.new_engine()

    def new_engine(self, **options: Any) -> approvals.ApprovalWorkflowEngine:
        return approvals.ApprovalWorkflowEngine(
            container_client=self.container, transport=self.transport, clock=lambda: self.now, **options,
        )

    async def initiate(self, **changes: Any) -> approvals.ApprovalContract:
        return await self.engine.initiate_approval(**{**copy.deepcopy(CONTEXT), **changes})

    def callback(self, contract: approvals.ApprovalContract, **changes: Any) -> dict[str, Any]:
        return {
            "approval_id": contract.approval_id, "environment": contract.environment,
            "request_hash": contract.request_hash, "decision": "approve",
            "approved_by": APPROVER, "approver_tenant_id": TENANT,
            "comment": "Reviewed", "workflow_run_id": "workflow-run-1",
            **changes,
        }

    def stored(self, contract: approvals.ApprovalContract) -> dict[str, Any]:
        return self.container.docs[(contract.environment, contract.approval_id)]

    def test_detection_is_static_deterministic_and_broad(self) -> None:
        for task in (
            "Build a ci/cd workflow", "Build CI-CD", "Add CICD", "CI CD setup",
            "continuous integration", "Continuous Delivery", "Create a PIPELINE",
            "Update Kubernetes", "Configure k8s", "deploy the service", "deploying API",
            "redeploy service", "re-deploy service", "deployment to prod", "roll out API",
            "rollback the release", "kubectl apply", "helm upgrade",
        ):
            with self.subTest(task=task):
                self.assertTrue(approvals.ApprovalWorkflowEngine.requires_approval(task))
        for task in ("Analyze customer churn", "Write a unit test", "Summarize this document"):
            self.assertFalse(approvals.ApprovalWorkflowEngine.requires_approval(task))
        for task in (None, "", " " * 10, "x" * 8193):
            with self.assertRaises(approvals.ApprovalValidationError):
                approvals.ApprovalWorkflowEngine.requires_approval(task)

    async def test_persist_before_notify_payload_hash_and_202_never_approve(self) -> None:
        async def inspect_dispatch(payload: dict[str, Any]) -> dict[str, str]:
            doc = self.container.docs[(CONTEXT["environment"], payload["approval_id"])]
            self.assertEqual(doc["notification_status"], "pending")
            self.assertEqual(doc["decision"], "pending")
            self.assertEqual(payload["request_hash"], doc["request_hash"])
            self.assertEqual(payload["approvers"], doc["approvers"])
            return {"decision": "approved"}  # A transport body is never a decision.

        self.transport.send.side_effect = inspect_dispatch
        contract = await self.initiate()
        payload = self.transport.send.call_args.args[0]
        expected_hash = hashlib.sha256(json.dumps(
            CONTEXT, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        self.assertEqual(contract.request_hash, expected_hash)
        self.assertEqual((contract.decision, contract.agent_validation), ("pending", "pending"))
        self.assertEqual(contract.notification_status, "sent")
        self.assertFalse(contract.is_complete())
        self.assertEqual(payload["callback_url"], CALLBACK)
        self.assertNotEqual(payload["callback_url"], WEBHOOK)
        self.assertEqual(payload["approvers"], [APPROVER, SECOND_APPROVER])
        self.assertNotIn("sig=", json.dumps(self.stored(contract)))
        self.assertEqual(
            approvals._parse_timestamp(contract.expires_at, "expires_at") - self.now,
            timedelta(hours=2),
        )
        self.assertTrue(all(partition == CONTEXT["environment"] for _, partition in self.container.reads))

    async def test_defaults_and_optional_fixed_callback(self) -> None:
        with patch.dict(os.environ, {"APPROVAL_CALLBACK_URL": ""}):
            contract = await self.engine.initiate_approval("Deploy API", "requester", "dev", "cluster")
        self.assertEqual(contract.namespace, "default")
        self.assertEqual(contract.image_tags, [])
        self.assertNotIn("callback_url", self.transport.send.call_args.args[0])
        self.assertIsNone(contract.commit_sha)

    def test_completion_requires_the_matching_validation_state(self) -> None:
        for decision in ("approved", "rejected", "timeout", "error"):
            contract = approvals.ApprovalContract(
                approval_id=OUTSIDER, task="Deploy", requested_by="requester",
                environment="dev", decision=decision,
            )
            self.assertFalse(contract.is_complete())
            contract.agent_validation = "passed" if decision in ("approved", "rejected") else "failed"
            self.assertTrue(contract.is_complete())

    async def test_resume_reads_shared_storage_and_does_not_notify(self) -> None:
        contract = await self.initiate()
        completed = await self.new_engine().process_approval_response(**self.callback(contract))
        resumed = await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)
        self.assertEqual(resumed.to_dict(), completed.to_dict())
        self.assertEqual((resumed.decision, resumed.agent_validation), ("approved", "passed"))
        self.transport.send.assert_awaited_once()
        self.assertEqual(len(self.container.docs), 1)

    async def test_contract_mutation_cannot_change_durable_state(self) -> None:
        contract = await self.initiate()
        contract.decision = "approved"
        contract.approvers.append(OUTSIDER)
        contract.image_tags.reverse()
        resumed = await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)
        self.assertEqual(resumed.decision, "pending")
        self.assertNotIn(OUTSIDER, resumed.approvers)
        self.assertEqual(resumed.image_tags, CONTEXT["image_tags"])

    async def test_resume_binds_every_immutable_field_including_requester(self) -> None:
        contract = await self.initiate()
        changes = {
            "task": CONTEXT["task"] + " ", "requested_by": CONTEXT["requested_by"].upper(),
            "cluster": "cluster-b", "namespace": "other", "image_tags": list(reversed(CONTEXT["image_tags"])),
            "commit_sha": "abc124", "pipeline_url": "https://pipeline.example/other",
            "rollback_url": None,
        }
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(approvals.ApprovalConflictError):
                await self.new_engine().resume_approval(contract.approval_id, **{**CONTEXT, key: value})
        self.transport.send.assert_awaited_once()
        self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_none_and_empty_context_are_not_silently_equivalent(self) -> None:
        contract = await self.initiate(commit_sha=None)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.resume_approval(contract.approval_id, **{**CONTEXT, "commit_sha": ""})

    async def test_unknown_approval_wrong_environment_and_hash_fail_closed(self) -> None:
        contract = await self.initiate()
        with self.assertRaises(approvals.ApprovalNotFoundError):
            await self.engine.resume_approval(contract.approval_id, **{**CONTEXT, "environment": "staging"})
        with self.assertRaises(approvals.ApprovalNotFoundError):
            await self.engine.process_approval_response(**self.callback(contract, environment="staging"))
        with self.assertRaises(approvals.ApprovalNotFoundError):
            await self.engine.get_approval(OUTSIDER, contract.environment)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.process_approval_response(**self.callback(contract, request_hash="0" * 64))
        with self.assertRaises(approvals.ApprovalValidationError):
            await self.engine.get_approval("' OR 1=1", contract.environment)
        self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_invalid_decisions_and_context_types_are_rejected(self) -> None:
        contract = await self.initiate()
        for decision in ("pending", "yes", "approve everything", "", None, True):
            with self.subTest(decision=decision), self.assertRaises(approvals.ApprovalValidationError):
                await self.engine.process_approval_response(**self.callback(contract, decision=decision))
        for changes in ({"image_tags": "image:1"}, {"image_tags": [True]}, {"task": "\ud800"}, {"namespace": ""}):
            with self.assertRaises(approvals.ApprovalValidationError):
                await self.initiate(**changes)

    async def test_stored_context_or_integrity_corruption_is_not_trusted(self) -> None:
        contract = await self.initiate()
        original = copy.deepcopy(self.stored(contract))
        for key, value in (
            ("task", "Deploy a different task"), ("request_hash", "0" * 64),
            ("_etag", ""), ("schema_version", 0), ("approvers", []),
            ("approval_tenant_id", OUTSIDER), ("expires_at", original["request_timestamp"]),
            ("decision", "approved"), ("environment", "other"),
        ):
            self.container.docs[(contract.environment, contract.approval_id)] = {**copy.deepcopy(original), key: value}
            with self.subTest(key=key), self.assertRaises(approvals.ApprovalStorageError):
                await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)
        self.container.docs[(contract.environment, contract.approval_id)] = original

    async def test_unauthorized_or_unsigned_display_name_never_approves(self) -> None:
        contract = await self.initiate()
        for changes in (
            {"approved_by": OUTSIDER}, {"approved_by": PRINCIPAL}, {"approved_by": "Jane Admin"},
            {"approved_by": "system"}, {"approved_by": ""}, {"approved_by": None},
            {"approver_tenant_id": OUTSIDER}, {"approver_tenant_id": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(approvals.ApprovalAuthorizationError):
                await self.engine.process_approval_response(**self.callback(contract, **changes))
        self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_approver_allowlist_is_the_stored_server_snapshot(self) -> None:
        contract = await self.initiate()
        with patch.dict(os.environ, {"APPROVAL_APPROVER_IDS": OUTSIDER}):
            with self.assertRaises(approvals.ApprovalAuthorizationError):
                await self.new_engine().process_approval_response(**self.callback(contract, approved_by=OUTSIDER))
            completed = await self.new_engine().process_approval_response(**self.callback(contract, approved_by=APPROVER.upper()))
        self.assertEqual(completed.approved_by, APPROVER)

    async def test_aliases_and_all_terminal_states(self) -> None:
        for alias, expected in (
            ("APPROVE", "approved"), ("approved", "approved"), (" Reject ", "rejected"),
            ("rejected", "rejected"), ("TIMEOUT", "timeout"), ("error", "error"),
        ):
            contract = await self.initiate()
            system = expected in ("timeout", "error")
            result = await self.engine.process_approval_response(**self.callback(
                contract, decision=alias, approved_by="" if system else APPROVER,
                approver_tenant_id=None if system else TENANT,
            ))
            self.assertEqual(result.decision, expected)
            self.assertEqual(result.agent_validation, "failed" if system else "passed")
            self.assertTrue(result.is_complete())

    async def test_identical_normalized_callback_is_idempotent_but_changes_conflict(self) -> None:
        contract = await self.initiate()
        original = await self.engine.process_approval_response(**self.callback(contract))
        version = self.stored(contract)["_etag"]
        duplicate = await self.new_engine().process_approval_response(**self.callback(contract, decision="approved", approved_by=APPROVER.upper()))
        self.assertEqual(original.to_dict(), duplicate.to_dict())
        self.assertEqual(self.stored(contract)["_etag"], version)
        for changes in (
            {"decision": "reject"}, {"comment": "Changed"}, {"workflow_run_id": "other-run"},
            {"approved_by": SECOND_APPROVER}, {"timestamp": self.now.isoformat()},
        ):
            with self.assertRaises(approvals.ApprovalConflictError):
                await self.engine.process_approval_response(**self.callback(contract, **changes))
        self.assertEqual(self.stored(contract)["_etag"], version)

    async def test_expiry_is_terminal_persisted_and_late_approval_is_rejected(self) -> None:
        contract = await self.initiate()
        earlier = self.now.isoformat()
        self.now += timedelta(hours=2)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.process_approval_response(**self.callback(contract, timestamp=earlier))
        expired = await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)
        self.assertEqual((expired.decision, expired.agent_validation), ("timeout", "failed"))
        self.assertTrue(expired.is_complete())
        version = self.stored(contract)["_etag"]
        self.now += timedelta(minutes=1)
        self.assertEqual((await self.engine.get_approval(contract.approval_id, contract.environment)).timestamp, expired.timestamp)
        self.assertEqual(self.stored(contract)["_etag"], version)

    async def test_timeout_callback_can_acknowledge_local_expiration_then_is_idempotent(self) -> None:
        contract = await self.initiate()
        self.now += timedelta(hours=3)
        expired = await self.engine.get_approval(contract.approval_id, contract.environment)
        body = self.callback(contract, decision="timeout", approved_by=None, approver_tenant_id=None)
        first = await self.engine.process_approval_response(**body)
        second = await self.new_engine().process_approval_response(**{**body, "approved_by": "system"})
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.timestamp, expired.timestamp)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.process_approval_response(**{**body, "workflow_run_id": "different"})

    async def test_expired_approved_result_can_be_acknowledged_but_not_resumed(self) -> None:
        contract = await self.initiate()
        body = self.callback(contract)
        completed = await self.engine.process_approval_response(**body)
        self.now += timedelta(hours=2)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)
        duplicate = await self.new_engine().process_approval_response(**body)
        self.assertEqual(duplicate.to_dict(), completed.to_dict())
        self.assertEqual(self.stored(contract)["decision"], "approved")

    async def test_callback_timestamp_is_validated_not_used_to_bypass_server_time(self) -> None:
        contract = await self.initiate()
        for timestamp in ("2026-09-06T12:00:00", "not-a-date", (self.now - timedelta(seconds=1)).isoformat(), (self.now + timedelta(minutes=6)).isoformat()):
            with self.assertRaises(approvals.ApprovalValidationError):
                await self.engine.process_approval_response(**self.callback(contract, timestamp=timestamp))
        self.now += timedelta(hours=1, minutes=59)
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.process_approval_response(**self.callback(contract, timestamp=contract.expires_at))
        first = await self.engine.process_approval_response(**self.callback(contract, timestamp="2026-09-06T12:00:00Z"))
        second = await self.engine.process_approval_response(**self.callback(contract, timestamp="2026-09-06T13:00:00+01:00"))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.timestamp, approvals._timestamp(self.now))

    async def test_missing_invalid_configuration_never_persists_or_notifies(self) -> None:
        for key, value in (
            ("APPROVAL_APPROVER_IDS", ""), ("APPROVAL_APPROVER_IDS", "someone@example.com"),
            ("APPROVAL_APPROVER_IDS", APPROVER + ","), ("APPROVAL_TIMEOUT_HOURS", "NaN"),
            ("APPROVAL_TIMEOUT_HOURS", "0"), ("APPROVAL_TIMEOUT_HOURS", "169"),
            ("AZURE_TENANT_ID", ""), ("APPROVAL_CALLBACK_AUDIENCE", ""),
            ("APPROVAL_CALLBACK_PRINCIPAL_ID", ""), ("LOGIC_APP_APPROVAL_WEBHOOK", ""),
            ("LOGIC_APP_APPROVAL_WEBHOOK", "https://workflow.example/unsigned"),
            ("APPROVAL_CALLBACK_URL", WEBHOOK), ("APPROVAL_CALLBACK_URL", "http://gateway.example/callback"),
            ("APPROVAL_CALLBACK_URL", "https://user:secret@gateway.example/callback"),
        ):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(approvals.ApprovalConfigurationError):
                    await self.new_engine().initiate_approval(**CONTEXT)
        self.assertFalse(self.container.docs)
        self.transport.send.assert_not_awaited()

    async def test_missing_storage_has_no_in_memory_fallback(self) -> None:
        engine = approvals.ApprovalWorkflowEngine(cosmos_endpoint="", transport=self.transport)
        with self.assertRaises(approvals.ApprovalConfigurationError):
            await engine.initiate_approval(**CONTEXT)
        self.transport.send.assert_not_awaited()
        approvals.get_agent_credential.assert_not_called()

    async def test_cosmos_uses_agent_credential_configured_container_and_worker_thread(self) -> None:
        credential = object()
        main_thread = threading.get_ident()
        credential_threads: list[int] = []

        def acquire() -> object:
            credential_threads.append(threading.get_ident())
            return credential

        cosmos = MagicMock()
        cosmos.get_database_client.return_value.get_container_client.return_value = self.container
        with patch.object(approvals, "get_agent_credential", side_effect=acquire), patch.object(approvals, "CosmosClient", return_value=cosmos) as factory:
            with patch.dict(os.environ, {"COSMOSDB_DATABASE_NAME": "customdb", "COSMOSDB_APPROVALS_CONTAINER": "customapprovals"}):
                engine = approvals.ApprovalWorkflowEngine(transport=self.transport, clock=lambda: self.now)
                await engine.initiate_approval(**CONTEXT)
            factory.assert_called_once_with(CONFIG["COSMOSDB_ENDPOINT"], credential=credential)
            cosmos.get_database_client.assert_called_once_with("customdb")
            cosmos.get_database_client.return_value.get_container_client.assert_called_once_with("customapprovals")
        self.assertTrue(credential_threads and all(value != main_thread for value in credential_threads))

    async def test_initial_storage_failure_never_notifies_and_is_secret_safe(self) -> None:
        self.container.fail_create = RuntimeError("fake-secret in a signed URL")
        with self.assertRaises(approvals.ApprovalStorageError) as failure:
            await self.initiate()
        self.transport.send.assert_not_awaited()
        self.assertFalse(self.container.docs)
        self.assertNotIn("fake-secret", "".join(traceback.format_exception(failure.exception)))

    async def test_notification_failure_is_a_durable_terminal_error(self) -> None:
        self.transport.send.side_effect = RuntimeError(WEBHOOK)
        contract = await self.initiate()
        self.assertEqual((contract.decision, contract.agent_validation), ("error", "failed"))
        self.assertEqual(contract.notification_status, "failed")
        self.assertEqual(contract.error_code, "notification_failed")
        self.assertNotIn("fake-secret", json.dumps(contract.to_dict()))
        self.assertEqual((await self.new_engine().resume_approval(contract.approval_id, **CONTEXT)).decision, "error")
        with self.assertRaises(approvals.ApprovalConflictError):
            await self.engine.process_approval_response(**self.callback(contract))

    async def test_notification_outcome_storage_failure_never_claims_sent(self) -> None:
        self.container.fail_replace = RuntimeError("fake-secret")
        with self.assertRaises(approvals.ApprovalStorageError):
            await self.initiate()
        self.transport.send.assert_awaited_once()
        doc = next(iter(self.container.docs.values()))
        self.assertEqual(doc["notification_status"], "pending")
        self.container.fail_replace = None
        resumed = await self.new_engine().resume_approval(doc["id"], **CONTEXT)
        with self.assertRaises(approvals.ApprovalInfrastructureError):
            await self.engine.process_approval_response(**self.callback(resumed))
        self.transport.send.assert_awaited_once()

    async def test_notification_and_recording_both_fail_closed(self) -> None:
        self.transport.send.side_effect = RuntimeError("fake-secret")
        self.container.fail_replace = RuntimeError("fake-secret")
        with self.assertRaises(approvals.ApprovalStorageError):
            await self.initiate()
        self.assertEqual(next(iter(self.container.docs.values()))["notification_status"], "pending")

    async def test_notification_failure_does_not_leak_into_a_later_conflict(self) -> None:
        async def expire_then_fail(payload: dict[str, Any]) -> None:
            self.now += timedelta(hours=2)
            await self.new_engine().get_approval(payload["approval_id"], payload["environment"])
            raise RuntimeError(WEBHOOK)

        self.transport.send.side_effect = expire_then_fail
        with self.assertRaises(approvals.ApprovalConflictError) as failure:
            await self.initiate()
        self.assertNotIn("fake-secret", "".join(traceback.format_exception(failure.exception)))
        self.assertIsNone(failure.exception.__context__)

    async def test_callback_before_dispatch_confirmation_is_retryable_not_approved(self) -> None:
        async def early_callback(payload: dict[str, Any]) -> None:
            body = {
                key: payload[key] for key in ("approval_id", "environment", "request_hash")
            }
            with self.assertRaises(approvals.ApprovalInfrastructureError):
                await self.new_engine().process_approval_response(
                    **body, decision="approve", approved_by=APPROVER,
                    approver_tenant_id=TENANT, workflow_run_id="early",
                )

        self.transport.send.side_effect = early_callback
        contract = await self.initiate()
        self.assertEqual(contract.decision, "pending")
        self.assertEqual((await self.engine.process_approval_response(**self.callback(contract))).decision, "approved")

    async def test_callback_storage_read_and_write_failures_are_not_success(self) -> None:
        contract = await self.initiate()
        for attribute in ("fail_read", "fail_replace"):
            setattr(self.container, attribute, RuntimeError("fake-secret"))
            with self.assertRaises(approvals.ApprovalStorageError) as failure:
                await self.engine.process_approval_response(**self.callback(contract))
            self.assertNotIn("fake-secret", str(failure.exception))
            setattr(self.container, attribute, None)
            self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_ambiguous_committed_write_reconciles_on_redelivery(self) -> None:
        contract = await self.initiate()
        self.container.fail_after_replace = TimeoutError("fake-secret")
        with self.assertRaises(approvals.ApprovalStorageError):
            await self.engine.process_approval_response(**self.callback(contract))
        version = self.stored(contract)["_etag"]
        result = await self.new_engine().process_approval_response(**self.callback(contract))
        self.assertEqual(result.decision, "approved")
        self.assertEqual(self.stored(contract)["_etag"], version)

    async def test_expiration_storage_failure_does_not_return_local_timeout(self) -> None:
        contract = await self.initiate()
        self.now += timedelta(hours=2)
        self.container.fail_replace = RuntimeError("unavailable")
        with self.assertRaises(approvals.ApprovalStorageError):
            await self.engine.get_approval(contract.approval_id, contract.environment)
        self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_identical_callbacks_racing_across_workers_share_one_terminal_write(self) -> None:
        contract = await self.initiate()
        self.container.race_next_two_reads()
        first, second = await asyncio.gather(
            self.engine.process_approval_response(**self.callback(contract)),
            self.new_engine().process_approval_response(**self.callback(contract)),
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(sum(doc["decision"] == "approved" for doc in self.container.replacements), 1)

    async def test_conflicting_callbacks_racing_across_workers_cannot_overwrite_winner(self) -> None:
        contract = await self.initiate()
        self.container.race_next_two_reads()
        results = await asyncio.gather(
            self.engine.process_approval_response(**self.callback(contract)),
            self.new_engine().process_approval_response(**self.callback(contract, decision="reject")),
            return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, approvals.ApprovalContract)]
        conflicts = [result for result in results if isinstance(result, approvals.ApprovalConflictError)]
        self.assertEqual((len(winners), len(conflicts)), (1, 1))
        self.assertEqual(self.stored(contract)["decision"], winners[0].decision)
        self.assertEqual(sum(doc["decision"] != "pending" for doc in self.container.replacements), 1)

    async def test_cas_retries_are_bounded_and_fail_closed(self) -> None:
        contract = await self.initiate()
        self.container.conflicts = 100
        with self.assertRaises(approvals.ApprovalStorageError):
            await self.engine.process_approval_response(**self.callback(contract))
        self.assertEqual(self.container.conflicts, 100 - approvals._CAS_ATTEMPTS)
        self.assertEqual(self.stored(contract)["decision"], "pending")

    async def test_singleton_and_compatibility_helper_never_auto_approve(self) -> None:
        with patch.object(approvals, "_workflow_engine", None):
            self.assertIs(approvals.get_approval_workflow_engine(), approvals.get_approval_workflow_engine())
        self.assertFalse(hasattr(approvals, "EntraAgentRegistryClient"))
        self.assertFalse(hasattr(approvals, "Agent365AvailabilityChecker"))
        with patch.object(approvals, "get_approval_workflow_engine", return_value=self.engine):
            context = {**CONTEXT, "task": "Analyze customer churn"}
            contract = await approvals.require_agents_approval(**context)
            resumed = await approvals.require_agents_approval(**context, approval_id=contract.approval_id)
        self.assertEqual(resumed.decision, "pending")
        self.transport.send.assert_awaited_once()

    async def test_real_transport_requires_202_and_15_second_deadline_without_redirects(self) -> None:
        for status in (202, 200, 201, 204, 302, 400, 401, 500):
            with self.subTest(status=status):
                response = MagicMock()
                response.__aenter__.return_value = response
                response.status = status
                response.text = AsyncMock()
                response.json = AsyncMock()
                session = MagicMock()
                session.__aenter__.return_value = session
                session.post.return_value = response
                with patch.object(approvals.aiohttp, "ClientSession", return_value=session) as factory:
                    client = approvals.LogicAppApprovalClient(WEBHOOK)
                    if status == 202:
                        await client.send({"approval_id": "test"})
                    else:
                        with self.assertRaises(approvals.ApprovalNotificationError):
                            await client.send({"approval_id": "test"})
                self.assertEqual(factory.call_args.kwargs["timeout"].total, 15)
                self.assertFalse(factory.call_args.kwargs["trust_env"])
                session.post.assert_called_once_with(WEBHOOK, json={"approval_id": "test"}, allow_redirects=False)
                response.text.assert_not_awaited()
                response.json.assert_not_awaited()

    async def test_real_transport_timeout_is_secret_safe(self) -> None:
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.side_effect = TimeoutError(WEBHOOK)
        with patch.object(approvals.aiohttp, "ClientSession", return_value=session):
            with self.assertRaises(approvals.ApprovalNotificationError) as failure:
                await approvals.LogicAppApprovalClient(WEBHOOK).send({"approval_id": "test"})
        self.assertNotIn("fake-secret", "".join(traceback.format_exception(failure.exception)))


if __name__ == "__main__":
    unittest.main()