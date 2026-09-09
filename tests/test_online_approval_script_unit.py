"""Offline tests for the opt-in CLI; no Azure, Teams, tokens, or live MCP calls.

The production runtime is deliberately not imported. All responses are authored
fixtures and all HTTP sends/socket connections are blocked unless replaced by an
explicit in-memory mock. Temporary files contain synthetic data only. These tests
do not establish deployed policy, callback authentication, or end-to-end readiness.
"""

from __future__ import annotations

import builtins
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
import requests

from scripts import test_online_agent365_approval as probe


ENDPOINT = "https://gateway.example/runtime/webhooks/mcp/message"
RUN_ID = "11111111-1111-4111-8111-111111111111"
APPROVAL_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
APPROVER_ID = "44444444-4444-4444-8444-444444444444"
TENANT_ID = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2035, 1, 2, 12, tzinfo=timezone.utc)
TASK = probe.synthetic_task(RUN_ID)
KEY = "unit-test-apim-secret"
TOKEN = "unit.test.bearer-secret"
REMOTE_SECRET = "remote-body-secret-never-print"
SIGNED_URL = "https://workflow.example/triggers/manual/invoke?sig=unit-signed-secret"
REAL_SESSION_SEND = requests.sessions.Session.send
CONTEXT_FIELDS = (
    "task", "requested_by", "environment", "cluster", "namespace", "image_tags",
    "commit_sha", "pipeline_url", "rollback_url",
)


def canonical_hash(value):
    # Independent encoding of the engine's documented request-context hash.
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def make_contract(decision="pending", **updates):
    contract = {
        "approval_id": APPROVAL_ID, "task": TASK, "requested_by": "offline-requester",
        "environment": "test", "cluster": "inert-example", "namespace": "default",
        "image_tags": [], "pipeline_url": SIGNED_URL,
        "decision": decision, "agent_validation": "pending",
        "request_timestamp": iso(NOW - timedelta(minutes=5)),
        "expires_at": iso(NOW + timedelta(hours=2)),
        "approvers": [APPROVER_ID], "approval_tenant_id": TENANT_ID,
        "notification_status": "sent", "notification_timestamp": iso(NOW - timedelta(minutes=4)),
    }
    if decision != "pending":
        contract.update(timestamp=iso(NOW), approved_by="system", agent_validation="failed")
    if decision in {"approved", "rejected"}:
        contract.update(
            agent_validation="passed", approved_by=APPROVER_ID,
            approver_tenant_id=TENANT_ID, workflow_run_id="offline-workflow-run",
        )
    if decision == "error":
        contract.update(notification_status="failed", error_code="notification_failed", comment=REMOTE_SECRET)
    contract.update(updates)
    contract["request_hash"] = canonical_hash({key: contract.get(key) for key in CONTEXT_FIELDS})
    return contract


def make_body(decision="pending", contract=None):
    contract = make_contract(decision) if contract is None else contract
    body = {
        "task": contract["task"], "approval_id": contract["approval_id"],
        "debug": {"token": REMOTE_SECRET, "callback_url": SIGNED_URL},
    }
    if decision == "approved":
        body.update(
            plan={"steps": [{"step": 1, "action": "Review this inert example recommendation"}], "total_steps": 1},
            metadata={"agents_approval_required": True, "approval_result": contract},
        )
    else:
        body.update(status="approval_" + decision, approval_contract=contract)
    return body


def schema_result(approval_schema=None):
    return {"tools": [{
        "name": "next_best_action", "inputSchema": {
            "type": "object", "properties": {
                "task": {"type": "string"},
                "approval_id": {"type": "string"} if approval_schema is None else approval_schema,
            }, "required": ["task"],
        },
    }]}


class Reply:
    def __init__(self, data=b"", *, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.data = data
        self.chunks = chunks
        self.iterated = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    @property
    def content(self):
        raise AssertionError("Unbounded response.content is forbidden")

    @property
    def text(self):
        raise AssertionError("Unbounded response.text is forbidden")

    def json(self):
        raise AssertionError("Unbounded response.json is forbidden")

    def iter_content(self, chunk_size):
        assert chunk_size == probe.CHUNK_BYTES
        chunks = self.chunks
        if chunks is None:
            chunks = (self.data[start:start + chunk_size] for start in range(0, len(self.data), chunk_size))
        for chunk in chunks:
            self.iterated += 1
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


def serve_result(wire, result, *, edit_envelope=None):
    replies = []

    def post(endpoint, **options):
        envelope = {"jsonrpc": "2.0", "id": options["json"]["id"], "result": deepcopy(result)}
        if edit_envelope is not None:
            edit_envelope(envelope)
        reply = Reply(json.dumps(envelope).encode("utf-8"))
        replies.append(reply)
        return reply

    wire.session.post.side_effect = post
    return replies


def serve_body(wire, body):
    return serve_result(wire, {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False})


def invoke(*args):
    return probe.main(["--endpoint", ENDPOINT, *(str(arg) for arg in args)])


def assert_one_post(wire, method, arguments=None):
    wire.factory.assert_called_once_with()
    wire.session.post.assert_called_once()
    positional, options = wire.session.post.call_args
    assert positional == (ENDPOINT,)
    assert options["allow_redirects"] is False
    assert options["verify"] is True and options["stream"] is True
    assert options["timeout"] == (5, 30)
    assert wire.session.trust_env is False and wire.session.verify is True
    assert wire.session.auth is None
    assert options["headers"]["Accept-Encoding"] == "identity"
    payload = options["json"]
    assert payload["jsonrpc"] == "2.0" and str(UUID(payload["id"])) == payload["id"]
    assert payload["method"] == method
    if arguments is None:
        assert "params" not in payload
    else:
        assert payload["params"] == {"name": "next_best_action", "arguments": arguments}
    for call in wire.session.mount.call_args_list:
        assert call.args[1].max_retries.total == 0


def assert_no_secrets(capsys):
    captured = capsys.readouterr()
    output = captured.out + captured.err
    for secret in (KEY, TOKEN, REMOTE_SECRET, SIGNED_URL, "unit-signed-secret"):
        assert secret not in output
    assert "Traceback" not in output
    return output


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("APIM_SUBSCRIPTION_KEY", raising=False)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(probe, "_utc_now", lambda: NOW)
    monkeypatch.setattr(probe, "uuid4", lambda: UUID(RUN_ID))
    blocked = Mock(side_effect=AssertionError("Unexpected network in offline unit tests"))
    monkeypatch.setattr(requests.sessions.Session, "send", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def wire(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    factory = Mock(return_value=session)
    monkeypatch.setattr(probe.requests, "Session", factory)
    return SimpleNamespace(session=session, factory=factory)


@pytest.fixture
def saved(wire, tmp_path):
    path = tmp_path / "approval.json"
    serve_body(wire, make_body())
    assert invoke("--initiate", "--state-file", path) == 0
    wire.session.post.reset_mock(side_effect=True)
    wire.factory.reset_mock()
    return path


def test_import_has_no_network_environment_reads_or_file_writes(monkeypatch):
    spec = importlib.util.spec_from_file_location("offline_approval_import_probe", probe.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    forbidden = Mock(side_effect=AssertionError("Import must not access services or state"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(requests, "Session", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    spec.loader.exec_module(module)
    assert module.__test__ is False
    forbidden.assert_not_called()


@pytest.mark.parametrize("action", [(), ("--preflight",)])
def test_default_and_explicit_preflight_are_list_only_and_write_nothing(wire, tmp_path, monkeypatch, action):
    replies = serve_result(wire, schema_result())
    before = list(tmp_path.iterdir())
    forbidden = Mock(side_effect=AssertionError("Preflight must not create tasks or state"))
    monkeypatch.setattr(probe, "_open_state", forbidden)
    monkeypatch.setattr(probe, "synthetic_task", forbidden)
    assert invoke(*action) == 0
    assert_one_post(wire, "tools/list")
    assert list(tmp_path.iterdir()) == before
    assert replies[0].closed
    forbidden.assert_not_called()


@pytest.mark.parametrize("schema", [
    {"type": "string"}, {"type": ["string", "null"]},
    {"anyOf": [{"type": "string"}, {"type": "null"}]},
])
def test_preflight_accepts_optional_string_approval_schema(wire, schema):
    serve_result(wire, schema_result(schema))
    assert invoke() == 0


@pytest.mark.parametrize("defect", ["missing-id", "wrong-id-type", "required-id", "missing-task", "duplicate-tool", "missing-tool"])
def test_preflight_fails_closed_on_schema_drift(wire, defect):
    result = schema_result()
    schema = result["tools"][0]["inputSchema"]
    if defect == "missing-id":
        del schema["properties"]["approval_id"]
    elif defect == "wrong-id-type":
        schema["properties"]["approval_id"] = {"type": "integer"}
    elif defect == "required-id":
        schema["required"].append("approval_id")
    elif defect == "missing-task":
        del schema["properties"]["task"]
    elif defect == "duplicate-tool":
        result["tools"].append(deepcopy(result["tools"][0]))
    else:
        result["tools"] = []
    serve_result(wire, result)
    assert invoke() == 1
    assert_one_post(wire, "tools/list")


@pytest.mark.parametrize("arguments", [
    [], ["--endpoint", ENDPOINT, "--initiate"], ["--endpoint", ENDPOINT, "--resume"],
    ["--endpoint", ENDPOINT, "--initiate", "--resume"],
    ["--endpoint", ENDPOINT, "--init"],
    ["--endpoint", ENDPOINT, "--token", TOKEN],
    ["--endpoint", ENDPOINT, "--expect", SIGNED_URL],
    ["--endpoint", ENDPOINT, "--approval-id", APPROVAL_ID],
    ["--endpoint", ENDPOINT, "--state-file", "unused.json"],
])
def test_invalid_cli_never_echoes_arguments_or_contacts_server(wire, arguments, capsys):
    assert probe.main(arguments) == 2
    wire.factory.assert_not_called()
    assert_no_secrets(capsys)


def test_initiation_cannot_expect_approval_or_override_id(wire, tmp_path):
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--expect", "approved", "--state-file", path) == 2
    assert invoke("--initiate", "--approval-id", APPROVAL_ID, "--state-file", path) == 2
    wire.factory.assert_not_called()
    assert not path.exists()


def test_help_is_static_and_does_not_read_credentials_or_state(wire, monkeypatch, capsys):
    forbidden = Mock(side_effect=AssertionError("Help must be inert"))
    monkeypatch.setattr(probe, "authentication_headers", forbidden)
    monkeypatch.setattr(probe, "_open_state", forbidden)
    with pytest.raises(SystemExit) as error:
        probe.main(["--help"])
    assert error.value.code == 0
    wire.factory.assert_not_called()
    forbidden.assert_not_called()
    assert "--initiate" in capsys.readouterr().out


def test_explicit_initiation_persists_only_safe_validated_fields(wire, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APIM_SUBSCRIPTION_KEY", KEY)
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    body = make_body()
    replies = serve_body(wire, body)
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--expect", "pending", "--state-file", path) == 0
    assert_one_post(wire, "tools/call", {"task": TASK})
    assert "Kubernetes deployment" in TASK and "do not execute" in TASK
    assert str(UUID(RUN_ID)) in TASK
    state = json.loads(path.read_text(encoding="utf-8"))
    assert set(state) == {
        "schema_version", "endpoint", "run_id", "task", "approval_id", "request_hash",
        "request_timestamp", "expires_at", "approver_snapshot_hash", "state_hash",
    }
    assert state["endpoint"] == ENDPOINT and state["task"] == TASK and state["run_id"] == RUN_ID
    assert state["approval_id"] == APPROVAL_ID
    assert state["request_hash"] == body["approval_contract"]["request_hash"]
    assert state["state_hash"] == canonical_hash({key: value for key, value in state.items() if key != "state_hash"})
    serialized = path.read_text(encoding="utf-8")
    for sensitive in (KEY, TOKEN, SIGNED_URL, REMOTE_SECRET, APPROVER_ID, "pipeline_url", "approval_contract", "headers"):
        assert sensitive not in serialized
    assert replies[0].closed
    assert_no_secrets(capsys)


def test_existing_state_prevents_another_initiation_before_any_network(wire, saved):
    original = saved.read_bytes()
    assert invoke("--initiate", "--state-file", saved) == 1
    assert saved.read_bytes() == original
    wire.factory.assert_not_called()


def test_initiation_reserves_state_before_sending_the_mutating_request(wire, tmp_path):
    path = tmp_path / "approval.json"
    serve_body(wire, make_body())
    respond = wire.session.post.side_effect

    def post(endpoint, **options):
        assert path.is_file() and path.read_bytes() == b""
        return respond(endpoint, **options)

    wire.session.post.side_effect = post
    assert invoke("--initiate", "--state-file", path) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["approval_id"] == APPROVAL_ID


def test_pending_contract_accepts_null_or_omitted_optional_fields(wire, tmp_path):
    contract = make_contract(approved_by=None, timestamp=None, commit_sha=None, rollback_url=None)
    serve_body(wire, make_body(contract=contract))
    assert invoke("--initiate", "--state-file", tmp_path / "approval.json") == 0


@pytest.mark.parametrize("defect", [
    "wrong-status", "missing-id", "bad-uuid", "mismatched-id", "bad-hash", "wrong-task",
    "passed", "approved-by", "timestamp", "missing-expiry", "missing-snapshot", "unacknowledged",
    "plan", "null-plan", "nested-plan", "legacy-wrapper",
])
def test_initiation_demands_the_exact_pending_contract(wire, tmp_path, capsys, defect):
    body = make_body()
    contract = body["approval_contract"]
    if defect == "wrong-status":
        body["status"] = "pending"
    elif defect == "missing-id":
        del body["approval_id"]
    elif defect == "bad-uuid":
        body["approval_id"] = contract["approval_id"] = "not-a-uuid"
    elif defect == "mismatched-id":
        body["approval_id"] = OTHER_ID
    elif defect == "bad-hash":
        contract["request_hash"] = "a" * 64
    elif defect == "wrong-task":
        contract["task"] = "Deploy something else"
    elif defect == "passed":
        contract["agent_validation"] = "passed"
    elif defect == "approved-by":
        contract["approved_by"] = APPROVER_ID
    elif defect == "timestamp":
        contract["timestamp"] = iso(NOW)
    elif defect == "missing-expiry":
        del contract["expires_at"]
    elif defect == "missing-snapshot":
        del contract["approvers"]
    elif defect == "unacknowledged":
        contract["notification_status"] = "pending"
    elif defect == "plan":
        body["plan"] = {"steps": [REMOTE_SECRET], "total_steps": 1}
    elif defect == "null-plan":
        body["plan"] = None
    elif defect == "nested-plan":
        body["metadata"] = {"plan": {"steps": [REMOTE_SECRET]}}
    else:
        body["approval_contract"] = {"approval_id": APPROVAL_ID, "approval_contract": contract}
    serve_body(wire, body)
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--state-file", path) == 1
    assert path.read_bytes() == b""  # Reservation prevents blind recreation after an uncertain result.
    assert_one_post(wire, "tools/call", {"task": TASK})
    assert_no_secrets(capsys)


def test_ambiguous_initiation_timeout_keeps_reservation_and_does_not_retry(wire, tmp_path, capsys):
    wire.session.post.side_effect = requests.Timeout(SIGNED_URL + TOKEN)
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--state-file", path) == 1
    assert path.read_bytes() == b""
    wire.session.post.assert_called_once()
    wire.factory.reset_mock()
    assert invoke("--initiate", "--state-file", path) == 1
    wire.factory.assert_not_called()
    assert_no_secrets(capsys)


@pytest.mark.parametrize("expect_argument", [(), ("--expect", "pending")])
def test_status_only_pending_resume_is_one_identical_call_with_no_recreation(wire, saved, expect_argument):
    original = saved.read_bytes()
    serve_body(wire, make_body())
    assert invoke("--resume", "--state-file", saved, *expect_argument) == 0
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})
    assert saved.read_bytes() == original


def test_explicit_repeat_resume_keeps_same_task_id_and_file(wire, saved):
    original = saved.read_bytes()
    serve_body(wire, make_body())
    for _ in range(2):
        assert invoke("--resume", "--state-file", saved, "--approval-id", APPROVAL_ID) == 0
    assert wire.session.post.call_count == 2  # Two explicit invocations, never an internal poll.
    for call in wire.session.post.call_args_list:
        assert call.kwargs["json"]["params"] == {
            "name": "next_best_action", "arguments": {"task": TASK, "approval_id": APPROVAL_ID},
        }
    assert saved.read_bytes() == original


@pytest.mark.parametrize("decision", ["approved", "rejected", "timeout", "error"])
def test_resume_asserts_each_full_lifecycle_contract_without_writing_state(wire, saved, capsys, decision):
    original = saved.read_bytes()
    serve_body(wire, make_body(decision))
    assert invoke("--resume", "--state-file", saved, "--expect", decision) == 0
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})
    assert saved.read_bytes() == original
    assert_no_secrets(capsys)


@pytest.mark.parametrize("status", ["approved", "success"])
def test_approved_can_include_an_explicit_success_status(wire, saved, status):
    body = make_body("approved")
    body["status"] = status
    serve_body(wire, body)
    assert invoke("--resume", "--state-file", saved, "--expect", "approved") == 0


@pytest.mark.parametrize("decision", ["pending", "rejected", "timeout", "error"])
def test_unapproved_plan_is_failure_even_when_status_is_expected(wire, saved, decision):
    body = make_body(decision)
    body["plan"] = {"steps": [{"action": "Must not be returned"}], "total_steps": 1}
    serve_body(wire, body)
    assert invoke("--resume", "--state-file", saved, "--expect", decision) == 1
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})


@pytest.mark.parametrize("defect", [
    "no-plan", "empty-plan", "empty-steps", "bad-step-count", "boolean-step-count",
    "no-metadata", "not-required", "truthy-required", "no-contract", "legacy-contract",
    "pending-validation", "failed-validation", "rejected-decision", "bad-hash", "changed-context",
    "no-top-id", "wrong-top-id", "wrong-contract-id", "changed-expiry", "changed-snapshot",
    "wrong-approver", "wrong-tenant", "missing-workflow", "ambiguous-status", "error-field",
])
def test_approved_requires_plan_passed_and_matching_hash_id_expiry_snapshot(wire, saved, capsys, defect):
    body = make_body("approved")
    contract = body["metadata"]["approval_result"]
    if defect == "no-plan":
        del body["plan"]
    elif defect == "empty-plan":
        body["plan"] = {}
    elif defect == "empty-steps":
        body["plan"] = {"steps": [], "total_steps": 0}
    elif defect == "bad-step-count":
        body["plan"]["total_steps"] = 2
    elif defect == "boolean-step-count":
        body["plan"]["total_steps"] = True
    elif defect == "no-metadata":
        del body["metadata"]
    elif defect == "not-required":
        body["metadata"]["agents_approval_required"] = False
    elif defect == "truthy-required":
        body["metadata"]["agents_approval_required"] = 1
    elif defect == "no-contract":
        del body["metadata"]["approval_result"]
    elif defect == "legacy-contract":
        body["metadata"]["approval_result"] = {"status": "approved", "approval_contract": contract}
    elif defect in {"pending-validation", "failed-validation"}:
        contract["agent_validation"] = defect.split("-", 1)[0]
    elif defect == "rejected-decision":
        contract["decision"] = "rejected"
    elif defect == "bad-hash":
        contract["request_hash"] = "f" * 64
    elif defect == "changed-context":
        contract["cluster"] = "different-cluster"
        contract["request_hash"] = canonical_hash({key: contract.get(key) for key in CONTEXT_FIELDS})
    elif defect == "no-top-id":
        del body["approval_id"]
    elif defect == "wrong-top-id":
        body["approval_id"] = OTHER_ID
    elif defect == "wrong-contract-id":
        body["approval_id"] = contract["approval_id"] = OTHER_ID
    elif defect == "changed-expiry":
        contract["expires_at"] = iso(NOW + timedelta(hours=3))
    elif defect == "changed-snapshot":
        contract["approvers"] = sorted([APPROVER_ID, OTHER_ID])
    elif defect == "wrong-approver":
        contract["approved_by"] = OTHER_ID
    elif defect == "wrong-tenant":
        contract["approver_tenant_id"] = OTHER_ID
    elif defect == "missing-workflow":
        del contract["workflow_run_id"]
    elif defect == "ambiguous-status":
        body["status"] = "approval_error"
    else:
        body["error"] = REMOTE_SECRET
    serve_body(wire, body)
    assert invoke("--resume", "--state-file", saved, "--expect", "approved") == 1
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})
    assert_no_secrets(capsys)


def test_previously_approved_but_now_expired_cannot_authorize_plan(wire, saved, monkeypatch):
    monkeypatch.setattr(probe, "_utc_now", lambda: NOW + timedelta(hours=2))
    serve_body(wire, make_body("approved"))
    assert invoke("--resume", "--state-file", saved, "--expect", "approved") == 1
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})


def test_expired_saved_request_can_still_be_resumed_to_assert_timeout(wire, saved, monkeypatch):
    later = NOW + timedelta(hours=3)
    monkeypatch.setattr(probe, "_utc_now", lambda: later)
    serve_body(wire, make_body("timeout", make_contract("timeout", timestamp=iso(later))))
    assert invoke("--resume", "--state-file", saved, "--expect", "timeout") == 0


def test_unexpected_approval_fails_pending_assertion_without_displaying_plan(wire, saved, capsys):
    body = make_body("approved")
    body["plan"]["steps"][0]["action"] = REMOTE_SECRET
    serve_body(wire, body)
    assert invoke("--resume", "--state-file", saved, "--expect", "pending") == 1
    assert_no_secrets(capsys)


@pytest.mark.parametrize("credentials", [{}, {"APIM_SUBSCRIPTION_KEY": KEY}, {"MCP_BEARER_TOKEN": TOKEN}, {
    "APIM_SUBSCRIPTION_KEY": KEY, "MCP_BEARER_TOKEN": TOKEN,
}])
def test_credentials_only_become_the_expected_headers(wire, monkeypatch, capsys, credentials):
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    serve_result(wire, schema_result())
    assert invoke() == 0
    headers = wire.session.post.call_args.kwargs["headers"]
    if "APIM_SUBSCRIPTION_KEY" in credentials:
        assert headers["Ocp-Apim-Subscription-Key"] == KEY
    else:
        assert "Ocp-Apim-Subscription-Key" not in headers
    if "MCP_BEARER_TOKEN" in credentials:
        assert headers["Authorization"] == "Bearer " + TOKEN
    else:
        assert "Authorization" not in headers
    body = json.dumps(wire.session.post.call_args.kwargs["json"])
    assert KEY not in body and TOKEN not in body
    assert_no_secrets(capsys)


@pytest.mark.parametrize(("name", "value"), [
    ("APIM_SUBSCRIPTION_KEY", ""), ("APIM_SUBSCRIPTION_KEY", KEY + "\r\nInjected: true"),
    ("APIM_SUBSCRIPTION_KEY", "non-ascii-\u00e9"), ("MCP_BEARER_TOKEN", "Bearer " + TOKEN),
    ("MCP_BEARER_TOKEN", TOKEN + "\n"), ("MCP_BEARER_TOKEN", "a" * 8193),
])
def test_invalid_credentials_fail_before_network_or_state_reservation(wire, tmp_path, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--state-file", path) == 1
    wire.factory.assert_not_called()
    assert not path.exists()
    assert_no_secrets(capsys)


def test_real_requests_preparation_ignores_netrc_proxies_and_environment_ca(monkeypatch, capsys):
    # Real requests preparation, but send itself is mocked before any I/O.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "must-not-be-opened.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "must-not-be-opened-either.pem")
    monkeypatch.setenv("APIM_SUBSCRIPTION_KEY", KEY)
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    netrc = Mock(side_effect=AssertionError("netrc must not be consulted"))
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc)

    def fake_send(prepared, **options):
        assert prepared.url == ENDPOINT
        assert prepared.headers["Authorization"] == "Bearer " + TOKEN
        assert prepared.headers["Ocp-Apim-Subscription-Key"] == KEY
        assert not options.get("proxies")
        assert options["verify"] is True and options["allow_redirects"] is False
        assert options["stream"] is True and options["timeout"] == probe.TIMEOUT
        sent = json.loads(prepared.body)
        return Reply(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": schema_result()}).encode())

    send = Mock(side_effect=fake_send)
    monkeypatch.setattr(requests.sessions.Session, "send", send)
    assert invoke() == 0
    send.assert_called_once()
    netrc.assert_not_called()
    assert_no_secrets(capsys)


@pytest.mark.parametrize("status", [202, 301, 302, 303, 307, 308, 400, 401, 403, 429, 500, 503])
def test_http_faults_and_redirects_do_not_read_body_follow_or_leak(wire, capsys, status):
    reply = Reply(REMOTE_SECRET.encode(), status=status, headers={"Location": SIGNED_URL, "WWW-Authenticate": TOKEN})
    wire.session.post.return_value = reply
    assert invoke() == 1
    assert_one_post(wire, "tools/list")
    assert reply.iterated == 0 and reply.closed
    assert_no_secrets(capsys)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 500])
def test_real_requests_never_reads_a_redirect_body_to_prepare_response_next(monkeypatch, capsys, status):
    # Exercise real Session.send + response-hook ordering, mocking only the
    # transport adapter. Merely mocking Session.post would miss this behavior.
    response = requests.Response()
    response.status_code = status
    response.headers["Location"] = SIGNED_URL
    response.raw = Mock()
    response.iter_content = Mock(side_effect=AssertionError("Redirect body must remain unread"))
    adapter_send = Mock(return_value=response)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", adapter_send)
    monkeypatch.setattr(requests.sessions.Session, "send", REAL_SESSION_SEND)
    assert invoke() == 1
    adapter_send.assert_called_once()
    response.iter_content.assert_not_called()
    response.raw.read.assert_not_called()
    response.raw.close.assert_called_once()
    assert_no_secrets(capsys)


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError, requests.exceptions.InvalidHeader])
def test_transport_errors_are_generic_and_never_retried(wire, capsys, error):
    wire.session.post.side_effect = error(SIGNED_URL + KEY + TOKEN)
    assert invoke() == 1
    assert_one_post(wire, "tools/list")
    assert_no_secrets(capsys)


def test_read_timeout_is_generic_closes_stream_and_never_retries(wire, capsys):
    reply = Reply(chunks=[b'{"secret":', requests.Timeout(SIGNED_URL + TOKEN)])
    wire.session.post.return_value = reply
    assert invoke() == 1
    assert reply.closed and reply.iterated == 2
    assert_one_post(wire, "tools/list")
    assert_no_secrets(capsys)


@pytest.mark.parametrize("raw", [
    b"not JSON", b"\xff", b"[]", b"null", b'{"result":NaN}',
    b'{"result":{},"result":{"secret":"remote-body-secret-never-print"}}',
    b'{"result":' + b"[" * 2000 + b"]" * 2000 + b"}",
])
def test_malformed_json_is_generic(wire, capsys, raw):
    wire.session.post.return_value = Reply(raw)
    assert invoke() == 1
    assert_one_post(wire, "tools/list")
    assert_no_secrets(capsys)


@pytest.mark.parametrize("defect", ["rpc-error", "wrong-id", "wrong-version", "missing-result", "iserror-true", "iserror-string", "iserror-int"])
def test_jsonrpc_faults_never_count_as_an_expected_approval_error(wire, saved, capsys, defect):
    def damage(envelope):
        if defect == "rpc-error":
            envelope["error"] = {"message": SIGNED_URL + REMOTE_SECRET}
        elif defect == "wrong-id":
            envelope["id"] = OTHER_ID
        elif defect == "wrong-version":
            envelope["jsonrpc"] = "1.0"
        elif defect == "missing-result":
            del envelope["result"]
        else:
            envelope["result"]["isError"] = {"iserror-true": True, "iserror-string": "false", "iserror-int": 1}[defect]
    result = {"content": [{"type": "text", "text": json.dumps(make_body("error"))}]}
    serve_result(wire, result, edit_envelope=damage)
    assert invoke("--resume", "--state-file", saved, "--expect", "error") == 1
    assert_one_post(wire, "tools/call", {"task": TASK, "approval_id": APPROVAL_ID})
    assert_no_secrets(capsys)


@pytest.mark.parametrize("content", [
    None, [], {}, [{"type": "image", "text": "{}"}], [{"type": "text", "text": {}}],
    [{"type": "text", "text": "not JSON " + REMOTE_SECRET}],
    [{"type": "text", "text": "__import__('os').system('must never run')"}],
    [{"type": "text", "text": '{"decision":"pending","decision":"approved"}'}],
    [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}],
])
def test_only_one_json_text_content_block_is_accepted(wire, saved, capsys, content):
    serve_result(wire, {"content": content})
    assert invoke("--resume", "--state-file", saved) == 1
    assert_no_secrets(capsys)


@pytest.mark.parametrize("headers", [
    {"Content-Length": str(probe.MAX_RESPONSE_BYTES + 1)},
    {"Content-Length": "-1"}, {"Content-Length": "not-a-number"},
    {"Content-Type": "text/event-stream"}, {"Content-Encoding": "gzip"},
])
def test_oversized_or_unsafe_headers_are_rejected_before_body_read(wire, capsys, headers):
    reply = Reply(REMOTE_SECRET.encode(), headers=headers)
    wire.session.post.return_value = reply
    assert invoke() == 1
    assert reply.iterated == 0 and reply.closed
    assert_no_secrets(capsys)


@pytest.mark.parametrize("advertised_length", [None, "1"])
def test_stream_is_bounded_even_without_or_with_false_content_length(wire, advertised_length):
    headers = {} if advertised_length is None else {"Content-Length": advertised_length}
    reply = Reply(headers=headers, chunks=[b" " * probe.MAX_RESPONSE_BYTES, b"x", b"must-not-be-read"])
    wire.session.post.return_value = reply
    assert invoke() == 1
    assert reply.iterated == 2 and reply.closed
    wire.session.post.assert_called_once()


def test_exactly_one_mib_valid_response_is_allowed(wire):
    envelope = {"jsonrpc": "2.0", "id": RUN_ID, "result": schema_result()}
    raw = json.dumps(envelope).encode()
    raw += b" " * (probe.MAX_RESPONSE_BYTES - len(raw))
    reply = Reply(raw, headers={"Content-Length": str(probe.MAX_RESPONSE_BYTES)})
    wire.session.post.return_value = reply
    assert invoke() == 0
    assert reply.closed


@pytest.mark.parametrize("endpoint", [
    ENDPOINT, "https://gateway.example:8443/mcp/messages",
    "https://127.0.0.1:8443/mcp/message", "https://[::1]:8443/mcp/message",
])
def test_https_endpoint_validation_does_not_rewrite_target(endpoint):
    assert probe.validate_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8000/runtime/webhooks/mcp/message",
    "http://127.0.0.2:8000/mcp/message", "http://[::1]:8000/mcp/message",
])
def test_http_requires_explicit_opt_in_and_literal_loopback(endpoint):
    with pytest.raises(probe.CheckFailed):
        probe.validate_endpoint(endpoint)
    assert probe.validate_endpoint(endpoint, allow_localhost=True) == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://gateway.example/mcp", "http://localhost:8000/mcp", "http://127.0.0.1.example/mcp",
    "http://0.0.0.0:8000/mcp", "http://[::]:8000/mcp", "http://192.168.1.2:8000/mcp",
    "http://127.1/mcp", "http://2130706433/mcp", "http://0177.0.0.1/mcp",
    "http://[::ffff:127.0.0.1]/mcp", "http://[::1%25eth0]/mcp",
    "http://[::1]untrusted.example/mcp", "https://[::1]suffix/mcp", "https://[v1.example]/mcp",
    "https://*.example/mcp", "https://0.0.0.0/mcp", "https://[::]/mcp",
    "https://user:password@gateway.example/mcp", "https://user@gateway.example/mcp",
    ENDPOINT + "?sig=unit-signed-secret", ENDPOINT + "?", ENDPOINT + "#fragment", ENDPOINT + "#",
    "https://gateway.example", "https://gateway.example/", "https://gateway.example/a/../mcp",
    "https://gateway.example/a/./mcp", "https://gateway.example//mcp",
    "https://gateway.example/%2fmcp", "https://gateway.example/mcp\\message",
    "https://gateway.example:0/mcp", "https://gateway.example:65536/mcp",
    "https://gateway.example:/mcp", "https://gateway.example:no/mcp",
    "https://gateway.example./mcp", "https://bad_host.example/mcp",
    "https://gateway.example/mcp\n", " https://gateway.example/mcp",
    "https://gateway.example/mcp\x00", "https://g\u00e1teway.example/mcp",
    "file:///tmp/mcp", "//gateway.example/mcp",
])
def test_unsafe_endpoint_rejected_before_session_even_with_loopback_flag(wire, endpoint, capsys):
    assert probe.main(["--endpoint", endpoint, "--allow-localhost"]) == 1
    wire.factory.assert_not_called()
    assert_no_secrets(capsys)


def test_loopback_opt_in_reaches_only_the_explicit_message_url(wire):
    endpoint = "http://127.0.0.1:8000/runtime/webhooks/mcp/message"
    serve_result(wire, schema_result())
    assert probe.main(["--endpoint", endpoint, "--allow-localhost"]) == 0
    assert wire.session.post.call_args.args == (endpoint,)
    assert wire.session.post.call_args.kwargs["verify"] is True


def reseal(state):
    state["state_hash"] = canonical_hash({key: value for key, value in state.items() if key != "state_hash"})


@pytest.mark.parametrize("defect", [
    "missing-id", "invalid-id", "nil-id", "uppercase-id", "changed-id", "changed-hash",
    "changed-endpoint", "resealed-endpoint", "changed-task", "resealed-task", "bad-run-id",
    "extra-secret-field", "bad-version", "boolean-version", "bad-checksum", "bad-expiry",
    "bad-request-hash", "bad-snapshot-hash",
])
def test_malformed_or_tampered_state_is_rejected_before_network(wire, saved, capsys, defect):
    state = json.loads(saved.read_text(encoding="utf-8"))
    if defect == "missing-id":
        del state["approval_id"]
    elif defect == "invalid-id":
        state["approval_id"] = SIGNED_URL
    elif defect == "nil-id":
        state["approval_id"] = str(UUID(int=0))
    elif defect == "uppercase-id":
        state["approval_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    elif defect == "changed-id":
        state["approval_id"] = OTHER_ID
    elif defect == "changed-hash":
        state["request_hash"] = "a" * 64
    elif defect in {"changed-endpoint", "resealed-endpoint"}:
        state["endpoint"] = "https://different.example/mcp"
    elif defect in {"changed-task", "resealed-task"}:
        state["task"] = "Deploy a real workload " + REMOTE_SECRET
    elif defect == "bad-run-id":
        state["run_id"] = "not-a-uuid"
    elif defect == "extra-secret-field":
        state["Authorization"] = TOKEN
    elif defect == "bad-version":
        state["schema_version"] = 2
    elif defect == "boolean-version":
        state["schema_version"] = True
    elif defect == "bad-checksum":
        state["state_hash"] = "b" * 64
    elif defect == "bad-expiry":
        state["expires_at"] = "2035-01-02T14:00:00"  # Naive timestamp.
    elif defect == "bad-request-hash":
        state["request_hash"] = REMOTE_SECRET
    else:
        state["approver_snapshot_hash"] = None
    if defect.startswith("resealed-"):
        reseal(state)
    saved.write_text(json.dumps(state), encoding="utf-8")
    original = saved.read_bytes()
    assert invoke("--resume", "--state-file", saved) == 1
    wire.factory.assert_not_called()
    assert saved.read_bytes() == original
    assert_no_secrets(capsys)


def test_independent_id_pin_rejects_a_rechecksummed_id_swap_before_network(wire, saved):
    state = json.loads(saved.read_text(encoding="utf-8"))
    state["approval_id"] = OTHER_ID
    reseal(state)
    saved.write_text(json.dumps(state), encoding="utf-8")
    assert invoke("--resume", "--state-file", saved, "--approval-id", APPROVAL_ID) == 1
    wire.factory.assert_not_called()


@pytest.mark.parametrize("pin", [OTHER_ID, "invalid", str(UUID(int=0))])
def test_mismatching_or_invalid_public_id_pin_is_rejected_before_network(wire, saved, pin):
    assert invoke("--resume", "--state-file", saved, "--approval-id", pin) == 1
    wire.factory.assert_not_called()


@pytest.mark.parametrize("raw", [
    b"", b"not JSON", b"\xff", b"[]", b"null", b'{"value":Infinity}',
    b'{"approval_id":"first","approval_id":"second"}', b" " * (probe.MAX_STATE_BYTES + 1),
])
def test_malformed_bounded_state_is_rejected_without_network(wire, saved, raw):
    saved.write_bytes(raw)
    assert invoke("--resume", "--state-file", saved) == 1
    wire.factory.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "directory", "relative", "stream", "unc", "missing-parent"])
def test_unsafe_state_paths_fail_before_network(wire, tmp_path, kind):
    if kind == "missing":
        path = tmp_path / "missing.json"
    elif kind == "directory":
        path = tmp_path / "directory.json"
        path.mkdir()
    elif kind == "relative":
        path = "relative.json"
    elif kind == "stream":
        path = str(tmp_path / "state.json") + ":stream.json"
    elif kind == "unc":
        path = "\\\\untrusted.invalid\\share\\state.json"
    else:
        path = tmp_path / "absent-parent" / "state.json"
    assert invoke("--resume", "--state-file", path) == 1
    wire.factory.assert_not_called()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "parent-symlink"])
def test_linked_state_is_rejected_without_reading_target_or_contacting_server(wire, saved, tmp_path, monkeypatch, kind):
    original = saved.read_bytes()
    link = tmp_path / "linked.json"
    try:
        if kind == "symlink":
            link.symlink_to(saved)
        elif kind == "hardlink":
            os.link(saved, link)
        else:
            directory = tmp_path / "directory-link"
            directory.symlink_to(tmp_path, target_is_directory=True)
            link = directory / saved.name
    except (OSError, NotImplementedError):
        pytest.skip("Local filesystem does not support unprivileged link creation")
    forbidden = Mock(side_effect=AssertionError("Linked state must not be opened"))
    monkeypatch.setattr(probe.os, "open", forbidden)
    assert invoke("--resume", "--state-file", link) == 1
    forbidden.assert_not_called()
    wire.factory.assert_not_called()
    assert saved.read_bytes() == original


def test_failed_state_write_is_generic_and_never_recreates_approval(wire, tmp_path, monkeypatch, capsys):
    serve_body(wire, make_body())
    monkeypatch.setattr(probe.os, "fsync", Mock(side_effect=OSError(SIGNED_URL + TOKEN)))
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--state-file", path) == 1
    wire.session.post.assert_called_once()
    assert path.exists()
    assert_no_secrets(capsys)


def test_interruption_never_retries_or_prints_exception_details(wire, tmp_path, capsys):
    wire.session.post.side_effect = KeyboardInterrupt(SIGNED_URL + TOKEN)
    path = tmp_path / "approval.json"
    assert invoke("--initiate", "--state-file", path) == 130
    assert path.read_bytes() == b""
    wire.session.post.assert_called_once()
    assert_no_secrets(capsys)