"""Deployment-only Agent Registry publication; NOT identity issuance or approvals.

Microsoft Learn contracts checked 2026-09-06 (see DOCUMENTATION below):
* agent365 (default): beta/copilot/agentRegistrations, POST / GET {id} / PATCH {id}.
  There is NO documented list/filter or separate card endpoint for this API.
  A durable creation journal is therefore mandatory unless adopting a saved ID.
* entra-beta (explicit compatibility option): beta/agentRegistry/agentInstances,
  with an inline agentCardManifest on creation and a separate card PATCH on reuse.
  This older API has an Agent 365 convergence notice; there is no automatic fallback.

Both publication contracts are PREVIEW, public-cloud only, and not supported by
Microsoft for production use. Do not confuse the GA package *inventory* API with
the registration API or infer that package IDs are registration IDs.

Input is a deliberately small, validated subset of the published registration
and public card schemas. No securityProfile, approval settings, tenant grants,
block/unblock operations, runtime credentials, or fabricated A2A endpoints.
Owners and skills are reconciled additively; deleting them requires admin review.
Serialize publishers for each tenant/source key. No documented server-side
uniqueness/transaction guarantee is assumed. A lost POST response never causes
an automatic second POST. A journal must be durable across deployment runners.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import requests
from azure.identity import AzureCliCredential


GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_SCOPE = GRAPH_ORIGIN + "/.default"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
REGISTRATION_PATH = "/beta/copilot/agentRegistrations"
INSTANCE_PATH = "/beta/agentRegistry/agentInstances"
CARD_PATH = "/beta/agentRegistry/agentCardManifests"
API_CHOICES = ("agent365", "entra-beta")
_REGISTRATION_DOC = (
    "https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/"
    "api/admin-settings/agent-registration/"
)
DOCUMENTATION = (
    _REGISTRATION_DOC + "overview",
    _REGISTRATION_DOC + "agentregistration-create",
    _REGISTRATION_DOC + "agentregistration-get",
    _REGISTRATION_DOC + "agentregistration-update",
    _REGISTRATION_DOC + "resources/agentregistration",
    "https://learn.microsoft.com/en-us/graph/api/agentregistry-post-agentinstances?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/agentregistry-list-agentinstances?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/agentinstance-update?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/agentinstance-list-agentcardmanifest?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/agentcardmanifest-update?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/graph/api/resources/agentskill?view=graph-rest-beta",
    "https://learn.microsoft.com/en-us/entra/agent-id/agent-registry-convergence",
)
PREVIEW_NOTICE = (
    "Agent Registry publication uses public-cloud beta APIs, not production-supported APIs. "
    "Identity issuance, registry metadata, package blocking and runtime approvals are separate."
)
CONSENT_ACTION = (
    "Have a tenant administrator consent the listed permissions on the DEPLOYMENT client, "
    "then refresh its az login. Azure RBAC Owner is not Graph consent. If the Azure CLI "
    "user login cannot obtain these scopes, use an administrator-approved deployment "
    "service-principal az login. Never grant publication permissions to the runtime agent."
)
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_PAGES = 100
_MAX_ITEMS = 10000


class RegistryError(RuntimeError):
    """Only locally authored, secret-safe messages; never wrap remote error text."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.http_status is not None:
            result["httpStatus"] = self.http_status
        return result


def guid(value: Any, label: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise RegistryError("invalid_configuration", f"{label} must be a GUID.") from None


def _text(value: Any, label: str, limit: int = 4096) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value) or "${" in value
    ):
        raise RegistryError("invalid_configuration", f"{label} must be nonempty resolved text.")
    return value


def registry_id(value: Any) -> str:
    # Both APIs declare String, not Guid. Legacy examples include ':' and spaces.
    value = _text(value, "Registry ID", 512)
    if value in (".", "..") or any(c in value for c in "/\\?#%"):
        raise RegistryError("invalid_configuration", "Invalid registry ID.")
    return value


def endpoint_url(value: Any) -> str:
    value = _text(value, "Endpoint URL", 2048)
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https" and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment and not any(c.isspace() for c in value)
            and "\\" not in value and (url.port is None or 0 < url.port < 65536)
        )
    except ValueError:
        valid = False
    if not valid:
        raise RegistryError(
            "invalid_configuration", "Use an absolute HTTPS endpoint without credentials, query or fragment; never publish a key/SAS URL."
        )
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "Source timestamp", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        raise RegistryError("invalid_configuration", "Source timestamps must be ISO 8601 with a timezone.") from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GraphClient:
    """Small synchronous Graph transport with bounded I/O and no redirect/retry.

    Default authentication is the existing Azure CLI deployment login, regardless
    of AZURE_CLIENT_ID, workload-identity or runtime-agent environment settings.
    Injected credentials/sessions are borrowed. Tokens and response bodies are
    never logged. JWT claims are diagnostic evidence from our credential, NOT a
    substitute for server authorization or for reading another principal's grants.
    """

    def __init__(self, *, credential=None, session=None, tenant_id: str | None = None):
        if os.getenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/") != "https://login.microsoftonline.com":
            raise RegistryError("unsupported_cloud", "Deployment Graph clients support Azure public cloud only.")
        self.tenant_id = guid(tenant_id, "Tenant ID") if tenant_id else None
        self._owns_credential = credential is None
        self._owns_session = session is None
        self.credential = credential if credential is not None else AzureCliCredential(
            tenant_id=self.tenant_id or "", process_timeout=10,
        )
        self.session = session if session is not None else requests.Session()
        self._token = None

    def access_token(self):
        if self._token is None or self._token.expires_on <= time.time() + 60:
            try:
                token = self.credential.get_token(GRAPH_SCOPE)
                if not isinstance(token.token, str) or not token.token or any(c.isspace() for c in token.token) or token.expires_on <= time.time():
                    raise ValueError()
                self._token = token
            except Exception:
                raise RegistryError(
                    "authentication_failed", "Cannot acquire a Graph token from the deployment az login; refresh the correct tenant login. No runtime credential fallback is allowed."
                ) from None
        return self._token

    def caller(self) -> dict[str, Any] | None:
        """Decode only the locally acquired token; opaque tokens are inconclusive."""
        token = self.access_token().token
        try:
            parts = token.split(".")
            if len(parts) != 3 or len(parts[1]) > 128 * 1024:
                return None
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(claims, dict) or claims.get("aud", "").rstrip("/") not in (GRAPH_ORIGIN, GRAPH_APP_ID):
                return None
            tenant = guid(claims.get("tid"), "Token tenant ID")
            if self.tenant_id and tenant != self.tenant_id:
                raise RegistryError("tenant_mismatch", "Deployment token belongs to a different tenant.")
            roles = claims.get("roles", [])
            scopes = claims.get("scp", "")
            if not isinstance(roles, list) or not all(isinstance(v, str) for v in roles) or not isinstance(scopes, str):
                return None
            return {
                "tenantId": tenant,
                "principalId": guid(claims.get("oid"), "Caller object ID"),
                "clientId": guid(claims.get("azp") or claims.get("appid"), "Caller client ID"),
                "permissionType": "delegated" if scopes else "application",
                "permissions": set(scopes.split()) if scopes else set(roles),
            }
        except RegistryError as error:
            if error.code == "tenant_mismatch":
                raise
            return None
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return None

    @staticmethod
    def safe_url(path: str) -> str:
        if not isinstance(path, str) or "\\" in path or any(c.isspace() or ord(c) < 32 for c in path):
            raise RegistryError("unsafe_graph_url", "Invalid Graph URL; request refused.")
        url = GRAPH_ORIGIN + path if path.startswith("/") and not path.startswith("//") else path
        try:
            parsed = urlsplit(url)
            decoded = unquote(parsed.path)
            valid = (
                parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and not parsed.fragment and parsed.path.startswith(("/beta/", "/v1.0/"))
                and not any(s in (".", "..") for s in decoded.split("/"))
                and "\\" not in decoded and "%" not in decoded
            )
        except ValueError:
            valid = False
        if not valid:
            raise RegistryError("unsafe_graph_url", "Graph URL must remain on the public Graph HTTPS origin and a supported version.")
        return url

    def request(
        self, method: str, path: str, *, body: dict | None = None,
        expected: tuple[int, ...] = (200,), empty_ok: bool = False,
        etag: str | None = None,
    ) -> dict[str, Any]:
        url = self.safe_url(path)  # Check before acquiring/attaching credentials.
        headers = {
            "Authorization": "Bearer " + self.access_token().token,
            "Accept": "application/json", "OData-Version": "4.0",
        }
        if etag is not None:
            if not isinstance(etag, str) or len(etag) > 1024 or any(ord(c) < 32 for c in etag):
                raise RegistryError("invalid_response", "Invalid Graph concurrency marker.")
            headers["If-Match"] = etag
        options: dict[str, Any] = {
            "headers": headers, "timeout": (5, 20), "allow_redirects": False, "stream": True,
        }
        if body is not None:
            options["json"] = body
        deadline = time.monotonic() + 30
        try:
            with self.session.request(method, url, **options) as response:
                status = int(response.status_code)
                if status not in expected:
                    code = "permission_denied" if status in (401, 403) else "graph_http_error"
                    raise RegistryError(
                        code, f"Graph request failed (HTTP {status}). Check deployment-client consent, tenant availability and Entra logs; response content is intentionally suppressed.",
                        http_status=status,
                    )
                if status == 204:
                    return {}
                content = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise RegistryError("response_limit", "Graph response exceeded its size or time limit.")
                if not content and empty_ok:
                    return {}
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError()
                return data
        except RegistryError:
            raise
        except requests.RequestException:
            raise RegistryError("transport_error", "Graph transport failed; a write may have completed. Reconcile before retrying.") from None
        except (ValueError, TypeError, UnicodeError):
            raise RegistryError("invalid_response", "Graph returned an invalid JSON object; response content is suppressed.") from None

    def collection(self, path: str, *, allow_singleton: bool = False) -> list[dict[str, Any]]:
        """Validate every continuation: exact origin AND original collection path."""
        url = self.safe_url(path)
        collection_path = urlsplit(url).path
        visited: set[str] = set()
        items: list[dict[str, Any]] = []
        while url:
            url = self.safe_url(url)
            if url in visited or len(visited) >= _MAX_PAGES or urlsplit(url).path != collection_path:
                raise RegistryError("unsafe_pagination", "Graph pagination repeated, changed collection, or exceeded the page limit.")
            visited.add(url)
            page = self.request("GET", url)
            if allow_singleton and len(visited) == 1 and "value" not in page and page.get("id") and "@odata.nextLink" not in page:
                return [page]
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
                raise RegistryError("invalid_response", "Graph collection is missing a valid value array.")
            items.extend(values)
            if len(items) > _MAX_ITEMS:
                raise RegistryError("response_limit", "Graph collection exceeded the item limit; use an explicit saved ID.")
            next_url = page.get("@odata.nextLink")
            if next_url is None:
                break
            if not isinstance(next_url, str) or not next_url.startswith("https://"):
                raise RegistryError("unsafe_pagination", "Graph continuation must be an absolute HTTPS URL.")
            url = next_url
        return items

    def close(self):
        self._token = None
        if self._owns_session:
            self.session.close()
        if self._owns_credential:
            self.credential.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


_INSTANCE_FIELDS = {
    "displayName", "description", "ownerIds", "managedByAppId", "sourceAgentId",
    "originatingStore", "agentIdentityId", "agentIdentityBlueprintId", "createdBy",
    "sourceCreatedDateTime", "sourceLastModifiedDateTime",
}
_CARD_FIELDS = {
    "name", "description", "version", "url", "documentationUrl", "skills",
    "defaultInputModes", "defaultOutputModes",
}
_SKILL_FIELDS = {"id", "name", "description", "tags", "examples", "inputModes", "outputModes"}


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise RegistryError("invalid_configuration", f"{label} must be a nonempty array (maximum 100 entries).")
    return list(dict.fromkeys(_text(v, label) for v in value))


def validate_publication(instance: dict, card: dict) -> tuple[dict, dict]:
    """Strict deployment-input allowlist, not a claim to implement all Graph fields."""
    if not isinstance(instance, dict) or set(instance) - _INSTANCE_FIELDS:
        raise RegistryError("invalid_configuration", "Unsupported registration input fields; use the supplied deployment template.")
    if not isinstance(card, dict) or set(card) - _CARD_FIELDS:
        raise RegistryError("invalid_configuration", "Unsupported card input fields; governance/approval/security settings are not publication metadata.")
    instance, card = deepcopy(instance), deepcopy(card)
    for key in ("displayName", "sourceAgentId", "originatingStore"):
        instance[key] = _text(instance.get(key), key, 512)
    instance["agentIdentityId"] = guid(instance.get("agentIdentityId"), "Agent identity ID")
    instance["ownerIds"] = list(dict.fromkeys(guid(v, "Owner object ID") for v in _string_list(instance.get("ownerIds"), "ownerIds")))
    for key in ("managedByAppId", "agentIdentityBlueprintId", "createdBy"):
        if key in instance:
            instance[key] = guid(instance[key], key)
    for key in ("sourceCreatedDateTime", "sourceLastModifiedDateTime"):
        if key in instance:
            instance[key] = _timestamp(instance[key])
    if "description" in instance:
        _text(instance["description"], "Description")
    for key in ("name", "description", "version"):
        card[key] = _text(card.get(key), "Card " + key)
    card["url"] = endpoint_url(card.get("url"))
    if "documentationUrl" in card:
        card["documentationUrl"] = endpoint_url(card["documentationUrl"])
    for key in ("defaultInputModes", "defaultOutputModes"):
        if key in card:
            card[key] = _string_list(card[key], key)
    if "skills" in card:
        if not isinstance(card["skills"], list) or not card["skills"] or len(card["skills"]) > 100:
            raise RegistryError("invalid_configuration", "Card skills must be a nonempty array (maximum 100 entries).")
        ids = set()
        for skill in card["skills"]:
            if not isinstance(skill, dict) or set(skill) - _SKILL_FIELDS:
                raise RegistryError("invalid_configuration", "Unsupported skill fields; inputSchema and approval settings are not card skills.")
            for key in ("id", "name", "description"):
                _text(skill.get(key), "Skill " + key)
            if skill["id"] in ids:
                raise RegistryError("ambiguous_input", "Card skill IDs must be unique.")
            ids.add(skill["id"])
            for key in ("tags", "examples", "inputModes", "outputModes"):
                if key in skill:
                    skill[key] = _string_list(skill[key], "Skill " + key)
    return instance, card


def _merge_owned(current: Any, desired: Any) -> Any:
    """Preserve unknown JSON members, unrelated skills and existing owners.

    agent365.agentCard is an opaque Json property: send its merged value, not a
    partial object that could erase unknown content. Arrays of skills merge by
    ID. An unrecognizable existing shape is a blocker, never a replacement.
    """
    if current is None:
        return deepcopy(desired)
    if isinstance(desired, dict):
        if not isinstance(current, dict):
            raise RegistryError("schema_drift", "Existing metadata has an incompatible object shape; manual review is required.")
        result = deepcopy(current)
        for key, value in desired.items():
            result[key] = _merge_owned(current.get(key), value)
        return result
    if isinstance(desired, list):
        if not isinstance(current, list):
            raise RegistryError("schema_drift", "Existing metadata has an incompatible array shape; manual review is required.")
        result = deepcopy(current)
        if desired and isinstance(desired[0], dict):
            if any(not isinstance(v, dict) or not isinstance(v.get("id"), str) or not v["id"] for v in current):
                raise RegistryError("schema_drift", "Existing skills cannot be reconciled safely by ID.")
            positions = {v["id"]: i for i, v in enumerate(current)}
            if len(positions) != len(current):
                raise RegistryError("ambiguous_record", "Existing card contains duplicate skill IDs.")
            for value in desired:
                if value["id"] in positions:
                    i = positions[value["id"]]
                    result[i] = _merge_owned(current[i], value)
                else:
                    result.append(deepcopy(value))
        else:
            if any(not isinstance(v, str) for v in current):
                raise RegistryError("schema_drift", "Existing metadata contains an incompatible string array.")
            result.extend(v for v in desired if v not in result)
        return result
    if type(current) is not type(desired):
        raise RegistryError("schema_drift", "Existing metadata has an incompatible scalar shape; manual review is required.")
    return deepcopy(desired)


def _delta(current: dict, desired: dict) -> dict:
    result = {}
    for key, value in desired.items():
        if key == "sourceLastModifiedDateTime" and current.get(key) is not None:
            # Graph can return fractional seconds or an equivalent UTC offset.
            # Formatting-only differences must not cause perpetual PATCHes.
            if _timestamp(current[key]) == _timestamp(value):
                continue
        merged = _merge_owned(current.get(key), value)
        if current.get(key) != merged:
            result[key] = merged
    return result


def _not_blocked(record: dict):
    # These fields are deliberately NOT in either outbound schema. Read an
    # administrative signal defensively if supplied by a preview tenant.
    if any(record.get(key) not in (None, False) for key in ("isBlocked", "isQuarantined")) or str(record.get("status", "")).lower() in ("blocked", "quarantined", "disabled"):
        raise RegistryError("administratively_blocked", "Agent metadata is blocked/quarantined. Administrator review is required; publication will not unblock it.")


class PublicationJournal:
    """Secret-free crash marker + saved ID. Never delete a pending marker to retry.

    Exclusive initial creation prevents two first POSTs from the same journal.
    Atomic replacement checkpoints the returned ID before any follow-up HTTP.
    A crash before that checkpoint requires explicit ID recovery/adoption.
    Separate runners must share this file and serialize deployment updates.
    """

    def __init__(self, path: str | Path, *, api: str, tenant_id: str, instance: dict):
        self.path = Path(path)
        source_key = hashlib.sha256(json.dumps(
            [instance["originatingStore"], instance["sourceAgentId"]], separators=(",", ":"),
        ).encode()).hexdigest()
        self.scope = {
            "version": 1, "api": api, "tenantId": guid(tenant_id, "Journal tenant ID"),
            "sourceKey": source_key, "agentIdentityId": instance["agentIdentityId"],
        }
        self.data: dict[str, Any] | None = None
        try:
            if self.path.exists():
                if self.path.stat().st_size > 16384:
                    raise ValueError()
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or any(data.get(k) != v for k, v in self.scope.items()):
                    raise ValueError()
                if set(data) - set(self.scope) - {"phase", "registryId"}:
                    raise ValueError()
                if data.get("phase") not in ("pending", "ready"):
                    raise ValueError()
                if data.get("phase") == "ready":
                    registry_id(data.get("registryId"))
                elif "registryId" in data:
                    raise ValueError()
                self.data = data
        except (OSError, ValueError, RegistryError):
            raise RegistryError("invalid_journal", "Publication journal is unreadable or belongs to a different tenant, API, source key or identity. Review it; do not overwrite it.") from None

    @property
    def saved_id(self) -> str | None:
        return self.data.get("registryId") if self.data else None

    def reserve(self):
        if self.data is not None:
            raise RegistryError("creation_outcome_unknown", "A publication attempt already exists. Recover its registration ID from Agent 365 and supply --registry-id; do not replay POST or delete the journal.")
        data = {**self.scope, "phase": "pending"}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("x", encoding="utf-8") as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            self.data = data
        except OSError:
            raise RegistryError("journal_unavailable", "Cannot exclusively reserve the publication journal. No POST was sent; check persistent storage or another publisher.") from None

    def discard_auth_rejection(self):
        """Release only our own pending marker after a definitive HTTP 401/403.

        These authentication/authorization rejections did not authorize creation.
        Conflicts, timeouts, malformed responses and all other outcomes retain
        the crash marker. There is still no automatic retry in this invocation.
        """
        expected = {**self.scope, "phase": "pending"}
        try:
            if self.data != expected or self.path.stat().st_size > 16384:
                raise ValueError()
            if json.loads(self.path.read_text(encoding="utf-8")) != expected:
                raise ValueError()
            self.path.unlink()
            self.data = None
        except (OSError, ValueError):
            raise RegistryError("journal_unavailable", "Graph rejected authorization, but its pending journal could not be safely released. Have an administrator verify rejection and review the journal before retrying.") from None

    def checkpoint(self, value: str):
        data = {**self.scope, "phase": "ready", "registryId": registry_id(value)}
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as output:
                temporary = output.name
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.data = data
        except OSError:
            raise RegistryError("journal_unavailable", "Registration ID could not be checkpointed. Preserve the returned registry ID and adopt it explicitly before retrying.") from None
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class AgentRegistryPublisher:
    """Reconcile metadata only. Never creates/changes an Entra identity or approval."""

    def __init__(self, graph: GraphClient, *, api: str = "agent365"):
        if api not in API_CHOICES:
            raise RegistryError("invalid_configuration", "Registry API must be agent365 or entra-beta.")
        self.graph, self.api = graph, api
        self.path = REGISTRATION_PATH if api == "agent365" else INSTANCE_PATH
        self.published_id: str | None = None

    def preflight(self, *, saved_id: str | None = None, managed_by: str | None = None) -> dict:
        """Check *publisher* consent; never mistake caller scopes for MI grants.

        No write is used as a probe. With no agent365 ID there is no documented
        read-only registration probe; availability remains an external prerequisite.
        GET alone never proves write consent. Opaque tokens fail inconclusively.
        """
        required = ["AgentRegistration.Read.All", "AgentRegistration.ReadWrite.All"] if self.api == "agent365" else [
            "AgentInstance.ReadWrite.All", "AgentCardManifest.ReadWrite.All",
        ]
        report: dict[str, Any] = {
            "component": "registryPublisher", "api": self.api, "status": "inconclusive",
            "requiredPermissions": required, "endpointCheck": "notRun",
            "notice": PREVIEW_NOTICE, "action": CONSENT_ACTION,
        }
        try:
            caller = self.graph.caller()
            if caller is None:
                report["action"] = "Deployment Graph token cannot be inspected. Permission readiness is inconclusive; use a deployment login with inspectable Graph permissions. " + CONSENT_ACTION
                return report
            report.update({k: caller[k] for k in ("tenantId", "principalId", "clientId", "permissionType")})
            missing = []
            for permission in required:
                alternatives = {permission}
                if self.api == "entra-beta" and caller["permissionType"] == "application" and managed_by == caller["clientId"]:
                    alternatives.add(permission.replace(".All", ".ManagedBy"))
                if not alternatives.intersection(caller["permissions"]):
                    missing.append(permission)
            report["missingPermissions"] = missing
            if missing:
                report["status"] = "missing_permissions"
                return report
            if saved_id:
                self._get(saved_id)
                report["endpointCheck"] = "readSucceeded"
            elif self.api == "entra-beta":
                page = self.graph.request("GET", self.path + "?$select=id")
                if not isinstance(page.get("value"), list):
                    raise RegistryError("invalid_response", "Registry endpoint did not return a collection.")
                report["endpointCheck"] = "readSucceeded"
            else:
                report["endpointCheck"] = "notAvailableWithoutSavedId"
            report["status"] = "satisfied"
            report["action"] = (
                "Consent evidence is present; no write authorization was exercised. Verify preview availability and tenant policies. "
                + ("Delegated entra-beta also requires Agent Registry Administrator (or a supported custom role). " if self.api == "entra-beta" else "")
                + "Publication does not enable, unblock or approve agent execution."
            )
        except RegistryError as error:
            report["error"] = error.as_dict()
            report["action"] = "Directory/API access could not be verified; this is NOT satisfied. " + CONSENT_ACTION
        return report

    def _get(self, value: str) -> dict:
        value = registry_id(value)
        record = self.graph.request("GET", self.path + "/" + quote(value, safe=""))
        if record.get("id") != value:
            raise RegistryError("invalid_response", "Registry response does not identify the requested record.")
        return record

    def _find_legacy(self, instance: dict) -> dict | None:
        matches: dict[str, dict] = {}
        # Local exact matching avoids relying on undocumented per-property filter
        # support. All pages are exhausted before deciding that there is one match.
        for record in self.graph.collection(INSTANCE_PATH):
            if record.get("sourceAgentId") == instance["sourceAgentId"] and record.get("originatingStore") == instance["originatingStore"]:
                value = registry_id(record.get("id"))
                if value in matches and matches[value] != record:
                    raise RegistryError("ambiguous_record", "Registry changed during discovery; serialize publishers and retry.")
                matches[value] = record
        if len(matches) > 1:
            raise RegistryError("ambiguous_record", "Multiple instances share the stable source key; review them and explicitly adopt --registry-id.")
        return self._get(next(iter(matches))) if matches else None

    @staticmethod
    def _check_identity(record: dict, desired: dict, *, adoption: bool):
        _not_blocked(record)
        for key in ("sourceAgentId", "originatingStore"):
            if record.get(key) != desired[key] and (record.get(key) or not adoption):
                raise RegistryError("source_mismatch", "Saved registry ID belongs to a different source key; no metadata was overwritten.")
        if record.get("agentIdentityId") and guid(record["agentIdentityId"], "Registered agent identity ID") != desired["agentIdentityId"]:
            raise RegistryError("identity_mismatch", "Registry record references a different identity; identity rebinding requires administrator review.")
        if desired.get("agentIdentityBlueprintId") and record.get("agentIdentityBlueprintId") and guid(record["agentIdentityBlueprintId"], "Registered blueprint object ID") != desired["agentIdentityBlueprintId"]:
            raise RegistryError("identity_mismatch", "Registry record references a different blueprint; rebinding requires administrator review.")
        manager = "managedByAppId" if "managedByAppId" in desired else "managedBy"
        if desired.get(manager) and record.get(manager) and desired[manager] != record[manager]:
            raise RegistryError("manager_mismatch", "Registry manager differs; ownership transfer is not a publication operation.")

    def _patch(self, path: str, desired: dict, read, check) -> dict:
        for attempt in range(3):
            current = read()
            check(current)
            delta = _delta(current, desired)
            if not delta:
                return current
            if self.api == "agent365" and path.startswith(REGISTRATION_PATH + "/"):
                delta["sourceLastModifiedDateTime"] = desired.get("sourceLastModifiedDateTime", _now())
            try:
                self.graph.request("PATCH", path, body=delta, expected=(200, 204), empty_ok=True, etag=current.get("@odata.etag"))
            except RegistryError as error:
                if error.http_status in (409, 412) and attempt < 2:
                    continue  # Reread/recompute, never replay a stale broad patch.
                raise
            current = read()
            check(current)
            if _delta(current, desired):
                raise RegistryError("reconciliation_pending", "Registry update is not yet observable. Rerun with the saved ID after propagation; no additional write was attempted.")
            return current
        raise RegistryError("conflict", "Concurrent registry updates could not be reconciled.")

    @staticmethod
    def _legacy_payload(instance: dict, card: dict) -> tuple[dict, dict]:
        result = {k: deepcopy(v) for k, v in instance.items() if k in (
            "displayName", "ownerIds", "sourceAgentId", "originatingStore", "agentIdentityId", "agentIdentityBlueprintId",
        )}
        if "managedByAppId" in instance:
            result["managedBy"] = instance["managedByAppId"]
        result.update(url=card["url"], preferredTransport="JSONRPC")
        manifest = {k: deepcopy(v) for k, v in card.items() if k not in ("name", "url", "skills")}
        manifest.update(displayName=card["name"], ownerIds=instance["ownerIds"], originatingStore=instance["originatingStore"])
        if "managedBy" in result:
            manifest["managedBy"] = result["managedBy"]
        if "skills" in card:
            manifest["skills"] = [{("displayName" if k == "name" else k): v for k, v in skill.items()} for skill in card["skills"]]
        return result, manifest

    def _legacy_card(self, value: str) -> dict | None:
        # Only a successful empty collection establishes absence. A 404 can also
        # mean hidden metadata or API unavailability: never blindly replace it.
        cards = self.graph.collection(INSTANCE_PATH + "/" + quote(value, safe="") + "/agentCardManifest", allow_singleton=True)
        if len(cards) > 1:
            raise RegistryError("ambiguous_record", "Instance has multiple card manifests; refusing an ambiguous update.")
        if cards:
            registry_id(cards[0].get("id"))
        return cards[0] if cards else None

    def _publish_legacy_card(self, value: str, instance: dict, desired: dict):
        card = self._legacy_card(value)
        if card is None:
            self._check_identity(self._get(value), instance, adoption=True)
            try:
                # Official instance PATCH creates the card if it does not exist.
                self.graph.request("PATCH", INSTANCE_PATH + "/" + quote(value, safe=""), body={"agentCardManifest": desired}, expected=(200, 204), empty_ok=True)
            except RegistryError as error:
                if error.http_status not in (409, 412):
                    raise
            card = self._legacy_card(value)
            if card is None:
                raise RegistryError("reconciliation_pending", "Card creation is not observable; rerun with the saved instance ID after propagation.")
        card_id = registry_id(card.get("id"))

        def read():
            self._check_identity(self._get(value), instance, adoption=True)
            current = self._legacy_card(value)
            if current is None or current.get("id") != card_id:
                raise RegistryError("ambiguous_record", "Card binding changed during publication; administrator review is required.")
            return current

        def check(current):
            _not_blocked(current)
            if current.get("originatingStore") not in (None, "", desired["originatingStore"]):
                raise RegistryError("source_mismatch", "Bound card belongs to a different originating store; it was not overwritten.")
            if desired.get("managedBy") and current.get("managedBy") and current["managedBy"] != desired["managedBy"]:
                raise RegistryError("manager_mismatch", "Bound card has a different managing application; ownership transfer requires administrator review.")

        self._patch(CARD_PATH + "/" + quote(card_id, safe=""), desired, read, check)
        return card_id

    def publish(
        self, instance: dict, card: dict, *, saved_id: str | None = None,
        journal_path: str | Path | None = None,
    ) -> dict[str, str]:
        self.published_id = None
        instance, card = validate_publication(instance, card)
        caller = self.graph.caller()
        if caller is None:
            raise RegistryError("inconclusive_permissions", "Deployment Graph permissions cannot be inspected. " + CONSENT_ACTION)
        if instance["agentIdentityId"] in (caller["principalId"], caller["clientId"]):
            raise RegistryError("runtime_identity_not_allowed", "Publication must use a separate deployment principal, never the runtime agent identity.")
        journal = PublicationJournal(journal_path, api=self.api, tenant_id=caller["tenantId"], instance=instance) if journal_path else None
        if saved_id:
            saved_id = registry_id(saved_id)
        if journal and journal.saved_id:
            if saved_id and saved_id != journal.saved_id:
                raise RegistryError("journal_mismatch", "Explicit registry ID differs from the saved journal ID; no overwrite is allowed.")
            saved_id = journal.saved_id
        if journal and journal.data and not saved_id and self.api == "agent365":
            raise RegistryError("creation_outcome_unknown", "Prior POST outcome is unknown. Recover the registration ID and use --registry-id; no duplicate POST will be attempted.")
        if self.api == "agent365" and not saved_id and journal is None:
            raise RegistryError("journal_required", "Agent 365 has no documented registration list API. Supply a durable journal path for first creation or a saved --registry-id for adoption.")
        report = self.preflight(saved_id=saved_id, managed_by=instance.get("managedByAppId"))
        if report["status"] != "satisfied":
            needed = ", ".join(report.get("missingPermissions") or report["requiredPermissions"])
            raise RegistryError(
                "preflight_failed", "Registry publisher preflight did not pass. Required permissions: " + needed + ". " + report["action"],
                http_status=report.get("error", {}).get("httpStatus"),
            )

        adoption = saved_id is not None
        current = self._get(saved_id) if saved_id else (self._find_legacy(instance) if self.api == "entra-beta" else None)
        desired, manifest = self._legacy_payload(instance, card) if self.api == "entra-beta" else (instance, card)
        if current is None:
            if self.api == "agent365":
                created_at = instance.get("sourceCreatedDateTime", _now())
                payload = {
                    **desired, "createdBy": instance.get("createdBy", caller["principalId"]),
                    "sourceCreatedDateTime": created_at,
                    "sourceLastModifiedDateTime": instance.get("sourceLastModifiedDateTime", created_at),
                    "agentCard": manifest,
                }
                journal.reserve()
            else:
                payload = {**desired, "agentCardManifest": manifest}
            try:
                created = self.graph.request("POST", self.path, body=payload, expected=(201,))
                self.published_id = registry_id(created.get("id"))
                if journal:
                    journal.checkpoint(self.published_id)
                current = self._get(self.published_id)
            except RegistryError as error:
                if self.published_id:
                    raise
                if self.api == "entra-beta" and (error.http_status in (409, 412, 500, 502, 503, 504) or error.code == "transport_error"):
                    current = self._find_legacy(instance)
                    if current is None:
                        raise RegistryError("creation_outcome_unknown", "Creation outcome could not be reconciled by source key. Review the registry before another publication.") from None
                elif self.api == "agent365" and error.http_status in (401, 403):
                    journal.discard_auth_rejection()
                    raise RegistryError(
                        "permission_denied", "Graph rejected publication authentication/authorization. No automatic retry was attempted. " + CONSENT_ACTION,
                        http_status=error.http_status,
                    ) from None
                elif self.api == "agent365":
                    raise RegistryError(
                        "creation_outcome_unknown", "Registration POST did not yield a recoverable ID. Keep the pending journal; an administrator must verify the outcome and recover/adopt --registry-id before another attempt. No registration-list endpoint is assumed.",
                        http_status=error.http_status,
                    ) from None
                else:
                    raise

        value = registry_id(current.get("id"))
        self.published_id = value
        self._check_identity(current, desired, adoption=adoption)
        mutable = {k: v for k, v in desired.items() if k not in ("createdBy", "sourceCreatedDateTime")}
        if self.api == "agent365":
            mutable["agentCard"] = manifest
        self._patch(
            self.path + "/" + quote(value, safe=""), mutable,
            lambda: self._get(value),
            lambda record: self._check_identity(record, desired, adoption=adoption),
        )
        result = {"agentIdentityId": instance["agentIdentityId"]}
        if self.api == "entra-beta":
            result["agentInstanceId"] = value
            result["agentCardManifestId"] = self._publish_legacy_card(value, desired, manifest)
        else:
            result["agentRegistrationId"] = value
        if journal:
            journal.checkpoint(value)
        return result