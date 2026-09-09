"""Opt-in online checks of the Python Agent365 approval MCP contract.

No action (or --preflight) sends only tools/list. --initiate deliberately creates
ONE synthetic, UUID-labelled Kubernetes deployment *planning* approval. It can
notify real Teams approvers; use only an authorized test environment. It neither
executes deployments nor approves requests. --resume sends exactly one tools/call
with the saved task and approval_id; --expect defaults to pending. There are no
retries, polling, callback POSTs, token acquisition, or implicit initialization.

Supply the complete MCP JSON-RPC message URL with --endpoint, not a service root
or SSE URL. HTTPS is required except for an explicitly opted-in literal loopback
HTTP URL for kubectl port-forward. Credentials, if needed, come ONLY from the
APIM_SUBSCRIPTION_KEY and/or MCP_BEARER_TOKEN environment variables. Environment
files, netrc, proxy settings, and environment-provided CA bundles are not loaded.
TLS verification remains enabled. No remote text, plan, headers, or exceptions
are printed. --expect error means a valid blocked approval_error contract, NOT
an HTTP, JSON-RPC, transport, or parsing failure.

Choose a new absolute .json --state-file in a private, local system TEMP
directory. Initiation exclusively reserves it BEFORE sending anything, then
persists only validated, allowlisted correlation fields. Existing files are
never overwritten. Failure/interruption can leave an empty/partial reservation
and an unknown server outcome: reconcile it server-side, do not blindly initiate
again. Resume never changes the file. Its checksum detects corruption/field
edits, not an attacker replacing and re-checksumming an entire valid file. Keep
the directory private; optional --approval-id independently pins the public ID.
Neither local state nor a checksum grants approval: the server must revalidate
the durable, same-context request on every resume.

Runtime assumptions: blocked results contain status=approval_<decision>, a
top-level approval_id, and the full approval_contract. Approved results contain
a nonempty plan.steps/total_steps, top-level approval_id, and
metadata.agents_approval_required=true plus metadata.approval_result containing
the FULL contract (not the legacy nested wrapper). Approved status may be absent,
approved, or success. Contracts follow ApprovalContract.to_dict(): null optional
fields may be absent. request_hash is SHA-256 of the canonical full server-owned
request context, not of the future recommendation. The approver snapshot and
expiry must remain unchanged. Only approved + passed + unexpired permits a plan.

Exit codes: 0 asserted contract satisfied; 1 failed/inconclusive; 2 invalid CLI
usage; 130 interrupted. Importing this module does not contact services or write
files. This file is a CLI, not an automatically executed pytest integration test.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Mapping
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import requests


__test__ = False
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_STATE_BYTES = 16 * 1024
TIMEOUT = (5, 30)
CHUNK_BYTES = 8192
_HASH = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_CONTEXT_FIELDS = (
    "task", "requested_by", "environment", "cluster", "namespace", "image_tags",
    "commit_sha", "pipeline_url", "rollback_url",
)
_STATE_FIELDS = frozenset({
    "schema_version", "endpoint", "run_id", "task", "approval_id", "request_hash",
    "request_timestamp", "expires_at", "approver_snapshot_hash", "state_hash",
})
_SUCCESS = {
    "pending": "PASS: pending; the same request remains blocked without a plan.",
    "approved": "PASS: approved, passed, unexpired; a plan was returned but not executed.",
    "rejected": "PASS: rejected; the same request is blocked without a plan.",
    "timeout": "PASS: timeout; the same request is blocked without a plan.",
    "error": "PASS: error; the same request is blocked without a plan.",
}


class CheckFailed(Exception):
    """A deliberately detail-free validation failure."""


class UsageError(Exception):
    """Do not echo argparse's message: invalid arguments can contain secrets."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError from None


def _require(condition: bool) -> None:
    if not condition:
        raise CheckFailed


def _uuid(value: Any, *, version: int | None = None) -> str:
    _require(isinstance(value, str) and len(value) == 36)
    try:
        parsed = UUID(value)
        _require(parsed.int != 0 and str(parsed) == value)
        _require(version is None or parsed.version == version)
    except (ValueError, TypeError, AttributeError):
        raise CheckFailed from None
    return value


def _hash(value: Any) -> str:
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None)
    return value


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> datetime:
    _require(isinstance(value, str) and 1 <= len(value) <= 64 and value.isascii())
    _require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value) is not None)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise CheckFailed from None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, maximum: int, *, empty: bool = False) -> None:
    _require(isinstance(value, str) and len(value) <= maximum)
    _require(empty or bool(value.strip()))
    _require(not any(ord(char) < 32 and char not in "\t\r\n" for char in value))
    value.encode("utf-8", errors="strict")


def validate_endpoint(endpoint: str, allow_localhost: bool = False) -> str:
    """Validate without DNS, URL rewriting, path inference, or credential reads.

    Unescaped ASCII paths are intentional: encoded separators, IDN ambiguity,
    userinfo, zone IDs, and URL normalization must not change the saved target.
    """
    _require(isinstance(endpoint, str) and 1 <= len(endpoint) <= 2048)
    _require(endpoint.isascii() and not any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in endpoint))
    _require(not any(char in endpoint for char in ("?", "#", "@", "\\", "%", "*")))
    try:
        parts = urlsplit(endpoint)
        _require(parts.scheme in {"https", "http"} and bool(parts.hostname))
        _require(endpoint.startswith(parts.scheme + "://"))
        _require(parts.username is None and parts.password is None)
        _require(not parts.query and not parts.fragment and not parts.netloc.endswith(":"))
        _require(parts.port is None or 1 <= parts.port <= 65535)
        _require(parts.path.startswith("/") and parts.path != "/")
        _require(re.fullmatch(r"/[A-Za-z0-9_~./-]+", parts.path) is not None)
        _require("//" not in parts.path and not {".", ".."}.intersection(parts.path.split("/")))
        host = parts.hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            _require(len(host) <= 253 and not host.endswith("."))
            _require(all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.split(".")))
        if address is not None:
            _require(not address.is_unspecified)
            # Python versions differ on whether IPv4-mapped IPv6 is_loopback.
            # Permit only unambiguous literal loopback addresses, never aliases.
            _require(address.version != 6 or address.ipv4_mapped is None)
        authority_host = f"[{host}]" if address is not None and address.version == 6 else host
        authority = authority_host + (f":{parts.port}" if parts.port is not None else "")
        # urlsplit alone tolerates text after a closing IPv6 bracket. Require
        # the complete authority to match the validated host/port, not a prefix.
        _require(parts.netloc.lower() == authority.lower())
        if parts.scheme == "http":
            _require(allow_localhost and address is not None and address.is_loopback)
    except (ValueError, TypeError):
        raise CheckFailed from None
    return endpoint


def authentication_headers(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Only these two explicit environment credentials; never acquire a token."""
    environ = os.environ if environ is None else environ
    headers = {"Accept": "application/json", "Content-Type": "application/json", "Accept-Encoding": "identity"}
    for name, header in (
        ("APIM_SUBSCRIPTION_KEY", "Ocp-Apim-Subscription-Key"),
        ("MCP_BEARER_TOKEN", "Authorization"),
    ):
        value = environ.get(name)
        if value is None:
            continue
        _require(isinstance(value, str) and 1 <= len(value) <= 8192)
        _require(value.isascii() and all(33 <= ord(char) <= 126 for char in value))
        if name == "MCP_BEARER_TOKEN":
            _require(_TOKEN.fullmatch(value) is not None)
            value = "Bearer " + value
        headers[header] = value
    return headers


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CheckFailed


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        _require(isinstance(value, dict))
        return value
    except Exception:
        raise CheckFailed from None


def _response_object(response: Any) -> dict[str, Any]:
    # Do not read error/redirect bodies, Location, cookies, or authentication hints.
    _require(response.status_code == 200)
    _require(response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json")
    # Reject compression even if a server ignores Accept-Encoding: identity.
    _require(response.headers.get("Content-Encoding", "identity").strip().lower() in {"", "identity"})
    length = response.headers.get("Content-Length")
    if length is not None:
        _require(isinstance(length, str) and re.fullmatch(r"[0-9]{1,7}", length) is not None)
        _require(int(length) <= MAX_RESPONSE_BYTES)
    data = bytearray()
    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
        _require(isinstance(chunk, bytes))
        _require(len(data) + len(chunk) <= MAX_RESPONSE_BYTES)
        data.extend(chunk)
    _require(length is None or len(data) == int(length))
    return _json_object(bytes(data))


def _reject_http_faults(response: requests.Response, **kwargs: Any) -> None:
    # requests may consume a redirect body to build Response.next even with
    # allow_redirects=False. A response hook runs BEFORE that implicit read.
    if response.status_code != 200:
        try:
            response.close()
        finally:
            raise CheckFailed from None


def _rpc(endpoint: str, method: str, params: dict[str, Any] | None, headers: dict[str, str]) -> dict[str, Any]:
    """One bounded POST. Never expose a requests exception or its chained context."""
    request_id = str(uuid4())
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.verify = True
            session.auth = None
            session.hooks["response"] = [_reject_http_faults]
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            with session.post(endpoint, json=payload, headers=headers, stream=True,
                              timeout=TIMEOUT, verify=True, allow_redirects=False) as response:
                envelope = _response_object(response)
        _require(envelope.get("jsonrpc") == "2.0" and envelope.get("id") == request_id)
        _require("error" not in envelope)
        result = envelope.get("result")
        _require(isinstance(result, dict) and result.get("isError", False) is False)
        return result
    except Exception:
        raise CheckFailed from None


def _string_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind == "string" or isinstance(kind, list) and "string" in kind and all(item in ("string", "null") for item in kind):
        return True
    alternatives = schema.get("anyOf")
    return isinstance(alternatives, list) and any(isinstance(item, dict) and item.get("type") == "string" for item in alternatives)


def _check_schema(result: dict[str, Any]) -> None:
    tools = result.get("tools")
    _require(isinstance(tools, list))
    candidates = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == "next_best_action"]
    _require(len(candidates) == 1)
    schema = candidates[0].get("inputSchema")
    _require(isinstance(schema, dict) and schema.get("type") == "object")
    properties = schema.get("properties")
    _require(isinstance(properties, dict))
    _require(_string_schema(properties.get("task")) and _string_schema(properties.get("approval_id")))
    _require(schema.get("required") == ["task"])


def _tool_body(result: dict[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    _require(isinstance(content, list) and len(content) == 1)
    block = content[0]
    _require(isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
    return _json_object(block["text"].encode("utf-8", errors="strict"))


def synthetic_task(run_id: str) -> str:
    _uuid(run_id, version=4)
    return (
        f"[Agent365 approval test {run_id}] Plan a synthetic Kubernetes deployment "
        "for an inert example workload. Recommendation only: do not execute any "
        "deployment, run commands, or modify infrastructure. Human approval of "
        "this request is required before generating the recommendation."
    )


def _contains_plan(value: Any) -> bool:
    remaining = [value]
    while remaining:
        item = remaining.pop()
        if isinstance(item, dict):
            if "plan" in item:
                return True
            remaining.extend(item.values())
        elif isinstance(item, list):
            remaining.extend(item)
    return False


def _snapshot_hash(contract: dict[str, Any]) -> str:
    return _digest({key: contract[key] for key in ("approvers", "approval_tenant_id")})


def _check_contract(contract: Any, task: str, decision: str, state: dict[str, Any] | None) -> None:
    _require(isinstance(contract, dict))
    required = {
        "approval_id", "task", "requested_by", "environment", "cluster", "namespace", "image_tags",
        "decision", "agent_validation", "request_hash", "request_timestamp", "expires_at",
        "approvers", "approval_tenant_id", "notification_status",
    }
    _require(required <= contract.keys() and contract["task"] == task and contract["decision"] == decision)
    _uuid(contract["approval_id"])
    context = {key: contract.get(key) for key in _CONTEXT_FIELDS}
    for key, maximum in (("task", 8192), ("requested_by", 256), ("cluster", 256), ("namespace", 253)):
        _text(context[key], maximum)
    _require(isinstance(context["environment"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context["environment"]) is not None)
    tags = context["image_tags"]
    _require(isinstance(tags, list) and len(tags) <= 100)
    for tag in tags:
        _text(tag, 512)
    for key, maximum in (("commit_sha", 128), ("pipeline_url", 2048), ("rollback_url", 2048)):
        if context[key] is not None:
            _text(context[key], maximum, empty=True)
    _require(hmac.compare_digest(_hash(contract["request_hash"]), _digest(context)))
    approvers = contract["approvers"]
    _require(isinstance(approvers, list) and 1 <= len(approvers) <= 100)
    _require(approvers == sorted({_uuid(value) for value in approvers}))
    _uuid(contract["approval_tenant_id"])
    requested, expires = _timestamp(contract["request_timestamp"]), _timestamp(contract["expires_at"])
    now = _utc_now()
    _require(requested < expires and expires - requested <= timedelta(days=7))
    _require(requested <= now + timedelta(minutes=5))
    _require(contract["notification_status"] in {"pending", "sent", "failed"})
    expected_validation = "pending" if decision == "pending" else "passed" if decision in {"approved", "rejected"} else "failed"
    _require(contract["agent_validation"] == expected_validation)
    if decision in {"pending", "approved", "rejected"}:
        _require(contract["notification_status"] == "sent")
        _require(requested <= _timestamp(contract.get("notification_timestamp")) < expires)
    if decision in {"pending", "approved"}:
        _require(now < expires)
    if decision == "pending":
        _require(all(contract.get(key) is None for key in (
            "approved_by", "timestamp", "approver_tenant_id", "workflow_run_id", "comment",
            "response_timestamp", "response_hash", "resolution_time_seconds", "error_code",
        )))
    else:
        completed = _timestamp(contract.get("timestamp"))
        _require(completed >= requested)
        if decision in {"approved", "rejected"}:
            _require(completed < expires and contract.get("approved_by") in approvers)
            _require(contract.get("approver_tenant_id") == contract["approval_tenant_id"])
            _text(contract.get("workflow_run_id"), 512)
        else:
            _require(contract.get("approved_by") == "system")
    if state is not None:
        for key in ("approval_id", "request_hash", "request_timestamp", "expires_at"):
            _require(contract[key] == state[key])
        _require(hmac.compare_digest(_snapshot_hash(contract), state["approver_snapshot_hash"]))


def _check_result(body: dict[str, Any], expected: str, task: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    _require("task" not in body or body["task"] == task)
    if expected == "approved":
        _require("status" not in body or body["status"] in {"approved", "success"})
        _require("error" not in body)
        metadata = body.get("metadata")
        _require(isinstance(metadata, dict) and metadata.get("agents_approval_required") is True)
        contract = metadata.get("approval_result")
        plan = body.get("plan")
        _require(isinstance(plan, dict) and isinstance(plan.get("steps"), list) and bool(plan["steps"]))
        _require(all(isinstance(step, dict) and bool(step) for step in plan["steps"]))
        _require(type(plan.get("total_steps")) is int and plan["total_steps"] == len(plan["steps"]))
        _require("approval_contract" not in body or body["approval_contract"] == contract)
    else:
        _require(body.get("status") == "approval_" + expected and not _contains_plan(body))
        contract = body.get("approval_contract")
        metadata = body.get("metadata")
        if isinstance(metadata, dict) and "approval_result" in metadata:
            _require(metadata["approval_result"] == contract)
    _check_contract(contract, task, expected, state)
    _require(body.get("approval_id") == contract["approval_id"])
    return contract


def _state_path(raw: str) -> Path:
    _require(isinstance(raw, str) and 1 <= len(raw) <= 4096)
    _require(not any(ord(char) < 32 or ord(char) == 127 for char in raw))
    _require(not raw.startswith(("\\\\", "//")))  # No UNC or Windows device paths.
    path = Path(raw)
    _require(path.is_absolute() and path.suffix.lower() == ".json" and ".." not in path.parts)
    if os.name == "nt":
        _require(re.match(r"^[A-Za-z]:[\\/]", raw) is not None and ":" not in raw[2:])
        for part in path.parts[1:]:
            _require(part == part.rstrip(" ."))
            _require(re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", part.split(".", 1)[0], re.IGNORECASE) is None)
    else:
        _require(":" not in raw)
    return path


def _not_reparse(info: os.stat_result) -> bool:
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return not stat.S_ISLNK(info.st_mode) and not reparse


def _open_state(path: Path, *, create: bool) -> BinaryIO:
    """Local regular files only; exclusive creation avoids accidental re-initiation.

    Parent checks and O_NOFOLLOW where available reduce link attacks, but cannot
    replace private directory ACLs against concurrent local file replacement.
    """
    for parent in reversed(path.parents):
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and _not_reparse(info))
    if not create:
        info = path.lstat()
        _require(stat.S_ISREG(info.st_mode) and _not_reparse(info) and info.st_nlink == 1)
        _require(0 < info.st_size <= MAX_STATE_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened, current = os.fstat(descriptor), path.lstat()
        _require(stat.S_ISREG(opened.st_mode) and _not_reparse(current) and opened.st_nlink == 1)
        _require((opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino))
        return os.fdopen(descriptor, "wb" if create else "rb")
    except Exception:
        os.close(descriptor)
        raise CheckFailed from None


def _make_state(endpoint: str, run_id: str, task: str, contract: dict[str, Any]) -> dict[str, Any]:
    # Never copy a complete response, contract, server URL, approver, or header.
    state = {"schema_version": 1, "endpoint": endpoint, "run_id": run_id, "task": task}
    for key in ("approval_id", "request_hash", "request_timestamp", "expires_at"):
        state[key] = contract[key]
    state["approver_snapshot_hash"] = _snapshot_hash(contract)
    state["state_hash"] = _digest(state)
    return state


def _read_state(path: Path, endpoint: str, allow_localhost: bool, approval_id: str | None) -> dict[str, Any]:
    if approval_id is not None:
        _uuid(approval_id)
    with _open_state(path, create=False) as source:
        raw = source.read(MAX_STATE_BYTES + 1)
    _require(len(raw) <= MAX_STATE_BYTES)
    state = _json_object(raw)
    _require(state.keys() == _STATE_FIELDS and type(state["schema_version"]) is int and state["schema_version"] == 1)
    _require(validate_endpoint(state["endpoint"], allow_localhost) == endpoint)
    _uuid(state["approval_id"])
    _require(approval_id is None or state["approval_id"] == approval_id)
    _require(state["task"] == synthetic_task(_uuid(state["run_id"], version=4)))
    for key in ("request_hash", "approver_snapshot_hash", "state_hash"):
        _hash(state[key])
    requested, expires = _timestamp(state["request_timestamp"]), _timestamp(state["expires_at"])
    _require(requested < expires and expires - requested <= timedelta(days=7))
    unsigned = {key: value for key, value in state.items() if key != "state_hash"}
    _require(hmac.compare_digest(state["state_hash"], _digest(unsigned)))
    return state


def _write_state(destination: BinaryIO, state: dict[str, Any]) -> None:
    raw = (json.dumps(state, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    _require(len(raw) <= MAX_STATE_BYTES)
    _require(destination.write(raw) == len(raw))
    destination.flush()
    os.fsync(destination.fileno())


def _parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(
        prog="agent365-approval-check", allow_abbrev=False,
        description="Check the Python MCP approval contract. No automatic approvals or deployments.",
        epilog=("Credentials only from APIM_SUBSCRIPTION_KEY and/or MCP_BEARER_TOKEN; none are acquired. "
                "Choose a new absolute .json state path in a private local system TEMP directory. "
                "A failed initiation can leave a reservation and an unknown server outcome; never blindly recreate it."),
    )
    parser.add_argument("--endpoint", required=True, help="Full HTTPS MCP JSON-RPC message URL; no credentials, query, or fragment.")
    parser.add_argument("--allow-localhost", action="store_true", help="Permit HTTP literal loopback only for an existing local kubectl port-forward.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--preflight", dest="action", action="store_const", const="preflight", help="Only tools/list; the default, with no approval mutation.")
    actions.add_argument("--initiate", dest="action", action="store_const", const="initiate", help="Opt in to ONE synthetic approval request, potentially notifying real Teams approvers.")
    actions.add_argument("--resume", dest="action", action="store_const", const="resume", help="ONE tools/call using the saved task and approval_id, without recreating or polling.")
    parser.set_defaults(action="preflight")
    parser.add_argument("--state-file", help="Absolute local .json path, required for initiate/resume; never overwritten.")
    parser.add_argument("--expect", choices=tuple(_SUCCESS), help="Lifecycle assertion for resume (default pending); initiate permits pending only.")
    parser.add_argument("--approval-id", help="Optional independent public UUID pin for resume; cannot override the saved ID.")
    return parser


def main(argv: list[str] | None = None) -> int:
    action = "preflight"
    try:
        args = _parser().parse_args(argv)
        action = args.action
        if action == "preflight" and any(value is not None for value in (args.state_file, args.expect, args.approval_id)):
            raise UsageError
        if action != "preflight" and args.state_file is None:
            raise UsageError
        if args.approval_id is not None and action != "resume":
            raise UsageError
        if action == "initiate" and args.expect not in (None, "pending"):
            raise UsageError
        endpoint = validate_endpoint(args.endpoint, args.allow_localhost)
        path = _state_path(args.state_file) if args.state_file is not None else None
        state = _read_state(path, endpoint, args.allow_localhost, args.approval_id) if action == "resume" else None
        headers = authentication_headers()
        if action == "preflight":
            _check_schema(_rpc(endpoint, "tools/list", None, headers))
            print("PASS: preflight schema supports approval_id; no approval was requested. This is not end-to-end validation.")
        elif action == "initiate":
            run_id = str(uuid4())
            task = synthetic_task(run_id)
            with _open_state(path, create=True) as destination:
                result = _rpc(endpoint, "tools/call", {"name": "next_best_action", "arguments": {"task": task}}, headers)
                contract = _check_result(_tool_body(result), "pending", task)
                _write_state(destination, _make_state(endpoint, run_id, task, contract))
            print("PASS: pending without a plan; safe state saved. Human review happens outside this CLI; resume with the same state file.")
        else:
            expected = args.expect or "pending"
            arguments = {"task": state["task"], "approval_id": state["approval_id"]}
            result = _rpc(endpoint, "tools/call", {"name": "next_best_action", "arguments": arguments}, headers)
            _check_result(_tool_body(result), expected, state["task"], state)
            print(_SUCCESS[expected])
        return 0
    except UsageError:
        print("FAIL: invalid CLI usage; use --help. Argument values are not echoed.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("FAIL: interrupted; no retry was made. Preserve any state reservation and reconcile the server outcome.", file=sys.stderr)
        return 130
    except Exception:
        if action == "initiate":
            print("FAIL: initiation was not verified; no retry was made. An approval may exist: keep any reserved state file and reconcile server-side.", file=sys.stderr)
        else:
            print("FAIL: approval check failed; no remote details are emitted and no retry was made.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())