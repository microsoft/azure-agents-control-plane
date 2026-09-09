"""Durable, fail-closed deployment approvals using Cosmos and a Logic App.

All public workflow operations are async. Synchronous Cosmos/credential work is
offloaded to threads; importing this module does not acquire credentials, access
Azure, or register agents. The container must already exist, partitioned by
/environment (or the legacy /partitionKey, whose value is also environment).

The initial HTTP 202 acknowledges dispatch, NEVER human approval. A caller must
resume with the same complete request context before proceeding, and require
decision=approved AND agent_validation=passed. There is no local approval cache,
Graph fallback, automatic re-notification, or automatic approval of other tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import aiohttp
from azure.core import MatchConditions
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions

if __package__:
    from .agent_identity import get_agent_credential
else:
    from agent_identity import get_agent_credential


_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HASH = re.compile(r"[0-9a-f]{64}")
_ENVIRONMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CAS_ATTEMPTS = 6
_SYSTEM_FIELDS = frozenset({"_etag", "_rid", "_self", "_attachments", "_ts"})


class ApprovalError(Exception):
    """A deliberately secret-safe error suitable for an API response."""

    status_code = 503


class ApprovalValidationError(ApprovalError, ValueError):
    status_code = 400


class ApprovalAuthorizationError(ApprovalError):
    status_code = 403


class ApprovalNotFoundError(ApprovalError):
    status_code = 404


class ApprovalConflictError(ApprovalError):
    status_code = 409


class ApprovalInfrastructureError(ApprovalError):
    status_code = 503


class ApprovalConfigurationError(ApprovalInfrastructureError):
    pass


class ApprovalStorageError(ApprovalInfrastructureError):
    pass


class ApprovalNotificationError(ApprovalInfrastructureError):
    pass


class ApprovalDecision(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ERROR = "error"


class AgentValidationStatus(Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


_HUMAN_DECISIONS = frozenset({"approved", "rejected"})
_TERMINAL_DECISIONS = frozenset({"approved", "rejected", "timeout", "error"})


def normalize_decision(decision: str) -> str:
    """Normalize only the documented Logic App decision aliases."""
    aliases = {
        "approve": "approved", "approved": "approved",
        "reject": "rejected", "rejected": "rejected",
        "timeout": "timeout", "error": "error",
    }
    if not isinstance(decision, str) or len(decision) > 16:
        raise ApprovalValidationError("Invalid approval decision.")
    result = aliases.get(decision.strip().lower())
    if result is None:
        raise ApprovalValidationError("Invalid approval decision.")
    return result


def _text(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str) or len(value) > maximum
        or (not empty and not value.strip())
        or any(ord(c) < 32 and c not in "\t\r\n" for c in value)
    ):
        raise ApprovalValidationError(f"Invalid {name}.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ApprovalValidationError(f"Invalid {name}.") from None
    return value


def _guid(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _GUID.fullmatch(value):
        raise ApprovalValidationError(f"{name} must be a GUID.")
    result = UUID(value)
    if result.int == 0:
        raise ApprovalValidationError(f"{name} must be a nonzero GUID.")
    return str(result)


def _environment(value: str) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT.fullmatch(value):
        raise ApprovalValidationError("Invalid environment.")
    return value


def _parse_timestamp(value: Any, name: str) -> datetime:
    value = _text(value, name, 64)
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ApprovalValidationError(f"{name} must be a timezone-aware ISO timestamp.") from None


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: dict[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _request_context(
    task: str, requested_by: str, environment: str, cluster: str,
    namespace: str = "default", image_tags: list[str] | None = None,
    commit_sha: str | None = None, pipeline_url: str | None = None,
    rollback_url: str | None = None,
) -> dict[str, Any]:
    """Preserve exact text and image order; only absent image_tags becomes []."""
    if image_tags is not None and (not isinstance(image_tags, list) or len(image_tags) > 100):
        raise ApprovalValidationError("Invalid image_tags.")
    return {
        "task": _text(task, "task", 8192),
        "requested_by": _text(requested_by, "requested_by", 256),
        "environment": _environment(environment),
        "cluster": _text(cluster, "cluster", 256),
        "namespace": _text(namespace, "namespace", 253),
        "image_tags": [_text(tag, "image_tags", 512) for tag in (image_tags or [])],
        "commit_sha": None if commit_sha is None else _text(commit_sha, "commit_sha", 128, empty=True),
        "pipeline_url": None if pipeline_url is None else _text(pipeline_url, "pipeline_url", 2048, empty=True),
        "rollback_url": None if rollback_url is None else _text(rollback_url, "rollback_url", 2048, empty=True),
    }


def _https_url(value: str, name: str, *, query: bool = False) -> str:
    try:
        _text(value, name, 8192)
        parts = urlsplit(value)
        if (
            parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or (parts.query and not query)
            or any(c.isspace() for c in value) or parts.port not in (None, 443)
        ):
            raise ValueError
    except (ValueError, ApprovalValidationError):
        raise ApprovalConfigurationError(f"{name} must be a valid HTTPS URL.") from None
    return value


@dataclass(frozen=True)
class CallbackAuthSettings:
    """Trusted server configuration, never derived from a token or x-headers."""

    tenant_id: str
    audience: str
    principal_id: str

    @property
    def audiences(self) -> tuple[str, str]:
        return (self.audience, self.audience.removeprefix("api://"))


def get_callback_auth_settings() -> CallbackAuthSettings:
    """Read the single tenant, blueprint audience, and Logic App MI object ID."""
    try:
        tenant = _guid(os.getenv("AZURE_TENANT_ID", "").strip(), "AZURE_TENANT_ID")
        audience_id = _guid(
            os.getenv("APPROVAL_CALLBACK_AUDIENCE", "").strip().removeprefix("api://"),
            "APPROVAL_CALLBACK_AUDIENCE",
        )
        principal = _guid(
            os.getenv("APPROVAL_CALLBACK_PRINCIPAL_ID", "").strip(),
            "APPROVAL_CALLBACK_PRINCIPAL_ID",
        )
    except ApprovalValidationError:
        raise ApprovalConfigurationError("Approval callback identity configuration is missing or invalid.") from None
    return CallbackAuthSettings(tenant, f"api://{audience_id}", principal)


def get_approver_tenant_id() -> str:
    """Pin the Teams human tenant independently of the callback/hosting tenant.

    No identity is inferred from callback input. An explicit empty/invalid
    setting fails closed; only absence retains the single-tenant default.
    """
    try:
        return _guid(os.getenv(
            "APPROVAL_APPROVER_TENANT_ID", os.getenv("AZURE_TENANT_ID", ""),
        ).strip(), "APPROVAL_APPROVER_TENANT_ID")
    except ApprovalValidationError:
        raise ApprovalConfigurationError("Approval human tenant configuration is missing or invalid.") from None


@dataclass
class ApprovalContract:
    approval_id: str
    requested_by: str
    task: str
    environment: str
    decision: str = ApprovalDecision.PENDING.value
    approved_by: str | None = None
    timestamp: str | None = None
    agent_validation: str = AgentValidationStatus.PENDING.value
    cluster: str | None = None
    namespace: str | None = None
    image_tags: list[str] | None = None
    commit_sha: str | None = None
    pipeline_url: str | None = None
    rollback_url: str | None = None
    comment: str | None = None
    request_timestamp: str | None = None
    expires_at: str | None = None
    request_hash: str | None = None
    approvers: list[str] = field(default_factory=list)
    approval_tenant_id: str | None = None
    approver_tenant_id: str | None = None
    workflow_run_id: str | None = None
    response_timestamp: str | None = None
    resolution_time_seconds: float | None = None
    notification_status: str = "pending"
    notification_timestamp: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return public state, never Cosmos metadata, tokens, or trigger URLs."""
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalContract:
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})

    def is_complete(self) -> bool:
        """Terminal does not mean authorized: rejection, timeout and error finish too."""
        return (
            self.decision in _HUMAN_DECISIONS and self.agent_validation == "passed"
        ) or (
            self.decision in {"timeout", "error"} and self.agent_validation == "failed"
        )


class LogicAppApprovalClient:
    """Single signed-trigger POST; no redirects, retries, polling, or Graph API."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = _https_url(webhook_url, "LOGIC_APP_APPROVAL_WEBHOOK", query=True)
        if not parse_qs(urlsplit(webhook_url).query).get("sig", [""])[0]:
            raise ApprovalConfigurationError("LOGIC_APP_APPROVAL_WEBHOOK must be a signed trigger URL.")

    async def send(self, payload: dict[str, Any]) -> None:
        """Accept only the immediate 202; never read or expose a response body."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), trust_env=False,
            ) as session:
                async with session.post(self._webhook_url, json=payload, allow_redirects=False) as response:
                    if response.status != 202:
                        raise ApprovalNotificationError("Approval notification was not acknowledged.")
        except Exception:
            # HTTP exceptions can include the signed trigger URL. Suppress their
            # chain as well as their text; do not log bodies, headers, or tokens.
            raise ApprovalNotificationError("Approval notification was not acknowledged.") from None


class ApprovalWorkflowEngine:
    """Cosmos is the only authoritative state, including dispatch and callback CAS.

    container_client, transport, and clock are explicit test seams, not fallbacks.
    Unconfirmed dispatch after a crash stays blocked until expiry; resume never
    sends again. Deployments should use a single Cosmos write region for CAS.
    """

    def __init__(
        self, cosmos_endpoint: str | None = None, cosmos_database: str | None = None,
        cosmos_container: str | None = None, logic_app_webhook_url: str | None = None,
        *, container_client: Any = None, transport: LogicAppApprovalClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cosmos_endpoint = os.getenv("COSMOSDB_ENDPOINT", "") if cosmos_endpoint is None else cosmos_endpoint
        self.cosmos_database = os.getenv("COSMOSDB_DATABASE_NAME", "mcpdb") if cosmos_database is None else cosmos_database
        self.cosmos_container = os.getenv("COSMOSDB_APPROVALS_CONTAINER", "approvals") if cosmos_container is None else cosmos_container
        self.logic_app_webhook_url = os.getenv("LOGIC_APP_APPROVAL_WEBHOOK", "") if logic_app_webhook_url is None else logic_app_webhook_url
        self._cosmos_container_client = container_client
        self._cosmos_client: Any = None
        self._credential: Any = None
        self._client_lock = threading.Lock()
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def requires_approval(task: str) -> bool:
        """Conservative deterministic gate, not a model decision or exact sentence."""
        _text(task, "task", 8192)
        return bool(re.search(
            r"\bci\s*[/_-]\s*cd\b|\bcicd\b|\bci\s+cd\b|"
            r"\bcontinuous\s+(?:integration|delivery|deployment)\b|"
            r"\bpipelines?\b|\bkubernetes\b|\bk8s\b|"
            r"\b(?:re[- ]?)?deploy(?:s|ed|ing|ment|ments)?\b|"
            r"\broll(?:out|back)\b|\broll\s+(?:out|back)\b|\bkubectl\b|\bhelm\b",
            task, flags=re.IGNORECASE,
        ))

    def _now(self) -> datetime:
        result = self._clock()
        if result.tzinfo is None or result.utcoffset() is None:
            raise ApprovalInfrastructureError("Approval clock is not timezone-aware.")
        return result.astimezone(timezone.utc)

    def _container(self) -> Any:
        """Called only in worker threads; never creates databases or containers."""
        if self._cosmos_container_client is not None:
            return self._cosmos_container_client
        with self._client_lock:
            if self._cosmos_container_client is None:
                _https_url(self.cosmos_endpoint, "COSMOSDB_ENDPOINT")
                if not all(
                    isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name)
                    for name in (self.cosmos_database, self.cosmos_container)
                ):
                    raise ApprovalConfigurationError("Cosmos approval database/container configuration is invalid.")
                self._credential = get_agent_credential()
                self._cosmos_client = CosmosClient(self.cosmos_endpoint, credential=self._credential)
                database = self._cosmos_client.get_database_client(self.cosmos_database)
                self._cosmos_container_client = database.get_container_client(self.cosmos_container)
        return self._cosmos_container_client

    def _validate_document(self, doc: Any, approval_id: str, environment: str) -> dict[str, Any]:
        """Reject legacy, corrupt, or incomplete state rather than trusting a flag."""
        tenant = get_approver_tenant_id()
        try:
            if (
                not isinstance(doc, dict) or doc.get("id") != approval_id
                or doc.get("approval_id") != approval_id or doc.get("environment") != environment
                or doc.get("partitionKey") != environment or doc.get("schema_version") != 1
                or not isinstance(doc.get("_etag"), str) or not doc["_etag"]
            ):
                raise ValueError
            context = _request_context(**{
                key: doc.get(key) for key in (
                    "task", "requested_by", "environment", "cluster", "namespace",
                    "image_tags", "commit_sha", "pipeline_url", "rollback_url",
                )
            })
            if not isinstance(doc.get("request_hash"), str) or not hmac.compare_digest(doc["request_hash"], _digest(context)):
                raise ValueError
            approvers = doc.get("approvers")
            if (
                not isinstance(approvers, list) or not 1 <= len(approvers) <= 100
                or approvers != sorted({_guid(item, "approvers") for item in approvers})
                or doc.get("approval_tenant_id") != tenant
            ):
                raise ValueError
            requested = _parse_timestamp(doc["request_timestamp"], "request_timestamp")
            expires = _parse_timestamp(doc["expires_at"], "expires_at")
            if expires <= requested or doc.get("notification_status") not in {"pending", "sent", "failed"}:
                raise ValueError
            decision = doc["decision"]
            if decision == "pending":
                if (
                    doc.get("agent_validation") != "pending" or doc.get("timestamp") is not None
                    or doc.get("response_hash") or doc["notification_status"] == "failed"
                ):
                    raise ValueError
            elif decision in _TERMINAL_DECISIONS:
                completed = _parse_timestamp(doc["timestamp"], "timestamp")
                expected = "passed" if decision in _HUMAN_DECISIONS else "failed"
                if doc.get("agent_validation") != expected or completed < requested:
                    raise ValueError
                if decision in _HUMAN_DECISIONS and (
                    doc.get("approved_by") not in approvers or doc.get("approver_tenant_id") != tenant
                    or doc.get("notification_status") != "sent" or completed >= expires
                    or not doc.get("response_hash") or not doc.get("workflow_run_id")
                ):
                    raise ValueError
            else:
                raise ValueError
            if doc.get("response_hash") is not None:
                if not isinstance(doc["response_hash"], str) or not _HASH.fullmatch(doc["response_hash"]):
                    raise ValueError
                _text(doc.get("workflow_run_id"), "workflow_run_id", 512)
                if doc.get("comment") is not None:
                    _text(doc["comment"], "comment", 4096, empty=True)
                response = {
                    key: doc.get(key) for key in (
                        "approval_id", "environment", "request_hash", "decision", "approved_by",
                        "approver_tenant_id", "comment", "workflow_run_id",
                    )
                }
                response["timestamp"] = doc.get("response_timestamp")
                if not hmac.compare_digest(doc["response_hash"], _digest(response)):
                    raise ValueError
        except (KeyError, TypeError, ValueError, ApprovalValidationError):
            raise ApprovalStorageError("Stored approval state failed integrity validation.") from None
        return doc

    async def _load(self, approval_id: str, environment: str) -> dict[str, Any]:
        try:
            doc = await asyncio.to_thread(
                lambda: self._container().read_item(item=approval_id, partition_key=environment)
            )
        except ApprovalError:
            raise
        except cosmos_exceptions.CosmosHttpResponseError as error:
            if error.status_code == 404:
                raise ApprovalNotFoundError("Approval was not found in this environment.") from None
            raise ApprovalStorageError("Approval storage read failed.") from None
        except Exception:
            raise ApprovalStorageError("Approval storage read failed.") from None
        return self._validate_document(doc, approval_id, environment)

    async def _create(self, doc: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(lambda: self._container().create_item(body=doc))
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalStorageError("Approval could not be persisted; no notification was sent.") from None
        return self._validate_document(result, doc["id"], doc["environment"])

    async def _replace(self, previous: dict[str, Any], updated: dict[str, Any]) -> dict[str, Any] | None:
        body = {key: value for key, value in updated.items() if key not in _SYSTEM_FIELDS}
        try:
            # The SDK obtains the partition key from the immutable body. The
            # ETag condition prevents overwriting a winner from another worker.
            result = await asyncio.to_thread(lambda: self._container().replace_item(
                item=previous["id"], body=body, etag=previous["_etag"],
                match_condition=MatchConditions.IfNotModified,
            ))
        except cosmos_exceptions.CosmosHttpResponseError as error:
            if error.status_code == 412:
                return None
            raise ApprovalStorageError("Approval state could not be persisted.") from None
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalStorageError("Approval state could not be persisted.") from None
        return self._validate_document(result, previous["id"], previous["environment"])

    def _terminal(self, doc: dict[str, Any], decision: str, now: datetime, **fields: Any) -> dict[str, Any]:
        return {
            **doc, "decision": decision,
            "agent_validation": "passed" if decision in _HUMAN_DECISIONS else "failed",
            "timestamp": _timestamp(now),
            "resolution_time_seconds": max(0, (now - _parse_timestamp(doc["request_timestamp"], "request_timestamp")).total_seconds()),
            **fields,
        }

    async def _record_notification(self, approval_id: str, environment: str, *, sent: bool) -> ApprovalContract:
        for _ in range(_CAS_ATTEMPTS):
            doc = await self._load(approval_id, environment)
            if doc["decision"] != "pending":
                raise ApprovalConflictError("Approval ended before notification was confirmed.")
            now = self._now()
            if sent and now >= _parse_timestamp(doc["expires_at"], "expires_at"):
                updated = self._terminal(doc, "timeout", now, approved_by="system", error_code="approval_expired")
            elif sent:
                updated = {**doc, "notification_status": "sent", "notification_timestamp": _timestamp(now)}
            else:
                updated = self._terminal(
                    doc, "error", now, approved_by="system", notification_status="failed",
                    error_code="notification_failed",
                )
            saved = await self._replace(doc, updated)
            if saved is not None:
                if saved["decision"] == "timeout":
                    raise ApprovalConflictError("Approval expired before notification was confirmed.")
                return ApprovalContract.from_dict(saved)
        raise ApprovalStorageError("Approval changed concurrently; retry reading its state.")

    async def initiate_approval(
        self, task: str, requested_by: str, environment: str, cluster: str,
        namespace: str = "default", image_tags: list[str] | None = None,
        commit_sha: str | None = None, pipeline_url: str | None = None,
        rollback_url: str | None = None,
    ) -> ApprovalContract:
        """Persist, dispatch once, persist dispatch outcome, return pending/error.

        Configuration and persistence failures raise ApprovalError (never a
        usable approval). A transport failure returns a durably recorded ERROR;
        if recording it also fails, a storage error is raised instead.
        """
        context = _request_context(task, requested_by, environment, cluster, namespace, image_tags, commit_sha, pipeline_url, rollback_url)
        get_callback_auth_settings()
        approver_tenant = get_approver_tenant_id()
        try:
            raw_approvers = os.getenv("APPROVAL_APPROVER_IDS", "").split(",")
            if not 1 <= len(raw_approvers) <= 100:
                raise ValueError
            approvers = sorted({_guid(value.strip(), "APPROVAL_APPROVER_IDS") for value in raw_approvers})
            timeout = float(os.getenv("APPROVAL_TIMEOUT_HOURS", "2"))
            if not math.isfinite(timeout) or not 0 < timeout <= 24:
                raise ValueError
        except (ValueError, ApprovalValidationError):
            raise ApprovalConfigurationError("Approval approvers or timeout configuration is missing or invalid.") from None
        # Validate even when a transport has been injected. Only trusted server
        # configuration can route a request or choose its callback target.
        configured_transport = LogicAppApprovalClient(self.logic_app_webhook_url)
        callback_url = os.getenv("APPROVAL_CALLBACK_URL", "")
        if callback_url:
            _https_url(callback_url, "APPROVAL_CALLBACK_URL")
            callback = urlsplit(callback_url)
            trigger = urlsplit(self.logic_app_webhook_url)
            if (
                (callback.netloc.lower(), callback.path.rstrip("/")) == (trigger.netloc.lower(), trigger.path.rstrip("/"))
                or "/triggers/" in callback.path.lower()
            ):
                raise ApprovalConfigurationError("APPROVAL_CALLBACK_URL must not be a workflow trigger.")
        now = self._now()
        contract = ApprovalContract(
            approval_id=str(uuid4()), **context, request_hash=_digest(context),
            request_timestamp=_timestamp(now), expires_at=_timestamp(now + timedelta(hours=timeout)),
            approvers=approvers, approval_tenant_id=approver_tenant,
        )
        await self._create({
            **contract.to_dict(), "id": contract.approval_id,
            "partitionKey": environment, "schema_version": 1,
        })
        payload = {
            **context, "approval_id": contract.approval_id, "request_hash": contract.request_hash,
            "request_timestamp": contract.request_timestamp, "expires_at": contract.expires_at,
            "approvers": list(approvers),
        }
        # The callback route is fixed in the workflow deployment. Never allow
        # a trigger payload to choose where the managed-identity token is sent.
        sent = False
        try:
            await (self._transport or configured_transport).send(payload)
            sent = True
        except Exception:
            pass
        # Do not retain a transport exception as the context of a later storage
        # or conflict error: that exception might contain the signed URL.
        return await self._record_notification(contract.approval_id, environment, sent=sent)

    async def get_approval(self, approval_id: str, environment: str) -> ApprovalContract:
        """Read audit state from Cosmos, durably expiring any overdue pending item."""
        approval_id = _guid(approval_id, "approval_id")
        environment = _environment(environment)
        for _ in range(_CAS_ATTEMPTS):
            doc = await self._load(approval_id, environment)
            now = self._now()
            if doc["decision"] != "pending" or now < _parse_timestamp(doc["expires_at"], "expires_at"):
                return ApprovalContract.from_dict(doc)
            updated = self._terminal(doc, "timeout", now, approved_by="system", error_code="approval_expired")
            saved = await self._replace(doc, updated)
            if saved is not None:
                return ApprovalContract.from_dict(saved)
        raise ApprovalStorageError("Approval changed concurrently; retry reading its state.")

    async def resume_approval(
        self, approval_id: str, task: str, requested_by: str, environment: str, cluster: str,
        namespace: str = "default", image_tags: list[str] | None = None,
        commit_sha: str | None = None, pipeline_url: str | None = None,
        rollback_url: str | None = None,
    ) -> ApprovalContract:
        """Resume only identical context; never notify, recreate, or extend expiry.

        Pending expiration returns a persisted TIMEOUT. An earlier APPROVED
        decision remains immutable for audit/idempotency but cannot be reused
        after expires_at: that resume raises ApprovalConflictError.
        """
        context = _request_context(task, requested_by, environment, cluster, namespace, image_tags, commit_sha, pipeline_url, rollback_url)
        contract = await self.get_approval(approval_id, environment)
        if not hmac.compare_digest(contract.request_hash or "", _digest(context)):
            raise ApprovalConflictError("Approval request context does not match.")
        if contract.decision == "approved" and self._now() >= _parse_timestamp(contract.expires_at, "expires_at"):
            raise ApprovalConflictError("Approval has expired and cannot authorize this request.")
        return contract

    async def process_approval_response(
        self, approval_id: str, *, environment: str, request_hash: str, decision: str,
        workflow_run_id: str, approved_by: str | None = None,
        approver_tenant_id: str | None = None, comment: str | None = None,
        timestamp: str | None = None,
    ) -> ApprovalContract:
        """Validate and CAS a normalized authenticated callback before returning.

        The HTTP adapter MUST authenticate the Logic App principal first. Human
        identity is then checked against the immutable stored user/tenant list.
        Exact normalized redelivery returns the original result, even after its
        expiry; it does not renew the request or authorize an expired resume.
        """
        approval_id = _guid(approval_id, "approval_id")
        environment = _environment(environment)
        decision = normalize_decision(decision)
        if not isinstance(request_hash, str) or not _HASH.fullmatch(request_hash):
            raise ApprovalValidationError("Invalid request_hash.")
        workflow_run_id = _text(workflow_run_id, "workflow_run_id", 512)
        if comment is not None:
            comment = _text(comment, "comment", 4096, empty=True)
        response_time = None if timestamp is None else _parse_timestamp(timestamp, "timestamp")
        try:
            if decision in _HUMAN_DECISIONS:
                approved_by = _guid(approved_by, "approved_by")
                approver_tenant_id = _guid(approver_tenant_id, "approver_tenant_id")
            else:
                if approved_by not in (None, "", "system"):
                    raise ApprovalValidationError("System decisions cannot name a human approver.")
                approved_by = "system"
                if approver_tenant_id in (None, ""):
                    approver_tenant_id = None
                else:
                    approver_tenant_id = _guid(approver_tenant_id, "approver_tenant_id")
        except ApprovalValidationError:
            raise ApprovalAuthorizationError("Callback responder identity is not authorized.") from None
        response = {
            "approval_id": approval_id, "environment": environment, "request_hash": request_hash,
            "decision": decision, "approved_by": approved_by, "approver_tenant_id": approver_tenant_id,
            "comment": comment, "workflow_run_id": workflow_run_id,
            "timestamp": None if response_time is None else _timestamp(response_time),
        }
        response_hash = _digest(response)
        for _ in range(_CAS_ATTEMPTS):
            doc = await self._load(approval_id, environment)
            if not hmac.compare_digest(doc["request_hash"], request_hash):
                raise ApprovalConflictError("Callback request_hash does not match.")
            if (
                (decision in _HUMAN_DECISIONS and approved_by not in doc["approvers"])
                or (approver_tenant_id is not None and approver_tenant_id != doc["approval_tenant_id"])
            ):
                raise ApprovalAuthorizationError("Callback responder identity is not authorized.")
            if doc.get("response_hash"):
                if hmac.compare_digest(doc["response_hash"], response_hash):
                    return ApprovalContract.from_dict(doc)
                raise ApprovalConflictError("Approval already has a different terminal response.")
            now = self._now()
            expired = now >= _parse_timestamp(doc["expires_at"], "expires_at")
            if doc["decision"] == "pending" and expired:
                saved = await self._replace(doc, self._terminal(
                    doc, "timeout", now, approved_by="system", error_code="approval_expired",
                ))
                if saved is None:
                    continue
                doc = saved
            # A local expiry has no workflow response yet. A first timeout
            # acknowledgment can attach its audit identity without changing the
            # terminal decision/time; all subsequent redeliveries use the hash.
            attaching_timeout = doc["decision"] == "timeout" and decision == "timeout"
            if doc["decision"] != "pending" and not attaching_timeout:
                raise ApprovalConflictError("Approval is already terminal or has expired.")
            if not attaching_timeout and doc["notification_status"] != "sent":
                raise ApprovalInfrastructureError("Approval notification is not durably confirmed; retry callback.")
            if response_time is not None:
                if response_time < _parse_timestamp(doc["request_timestamp"], "request_timestamp") or response_time > now + timedelta(minutes=5):
                    raise ApprovalValidationError("Callback timestamp is outside the request interval.")
                if decision in _HUMAN_DECISIONS and response_time >= _parse_timestamp(doc["expires_at"], "expires_at"):
                    raise ApprovalConflictError("Callback decision is outside the approval lifetime.")
            fields = {
                "approved_by": approved_by, "approver_tenant_id": approver_tenant_id,
                "comment": comment, "workflow_run_id": workflow_run_id,
                "response_timestamp": response["timestamp"], "response_hash": response_hash,
            }
            updated = {**doc, **fields} if attaching_timeout else self._terminal(doc, decision, now, **fields)
            saved = await self._replace(doc, updated)
            if saved is not None:
                return ApprovalContract.from_dict(saved)
        raise ApprovalStorageError("Approval changed concurrently; retry callback.")


_workflow_engine: ApprovalWorkflowEngine | None = None
_workflow_engine_lock = threading.Lock()


def get_approval_workflow_engine() -> ApprovalWorkflowEngine:
    """Return the process-local client singleton; approval state is NOT local."""
    global _workflow_engine
    with _workflow_engine_lock:
        if _workflow_engine is None:
            _workflow_engine = ApprovalWorkflowEngine()
        return _workflow_engine


async def require_agents_approval(
    task: str, requested_by: str, environment: str, cluster: str,
    *, approval_id: str | None = None, **deployment_context: Any,
) -> ApprovalContract:
    """Nonblocking compatibility checkpoint; never auto-approve or poll in-process.

    Without approval_id initiate once; with it resume. The caller must check
    both approved/passed before continuing, and present the same context.
    """
    engine = get_approval_workflow_engine()
    context = dict(task=task, requested_by=requested_by, environment=environment, cluster=cluster, **deployment_context)
    if approval_id is not None:
        return await engine.resume_approval(approval_id=approval_id, **context)
    return await engine.initiate_approval(**context)


__all__ = [
    "ApprovalContract", "ApprovalDecision", "AgentValidationStatus",
    "ApprovalWorkflowEngine", "LogicAppApprovalClient", "get_approval_workflow_engine",
    "require_agents_approval", "ApprovalError", "ApprovalValidationError",
    "ApprovalAuthorizationError", "ApprovalNotFoundError", "ApprovalConflictError",
    "ApprovalInfrastructureError", "ApprovalConfigurationError", "ApprovalStorageError",
]
