"""Offline Graph contract tests. No Azure CLI, network, grants or deployments.

These mocks validate the documented beta payload boundaries and retry behavior,
not service availability or production support. Run only in the parent's test pass.
"""

import base64
from copy import deepcopy
import json
from pathlib import Path
import traceback
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import pytest
from azure.core.credentials import AccessToken

from scripts import publish_agent_registry as cli
from src import agent_registry as registry


TENANT = "11111111-1111-1111-1111-111111111111"
CALLER = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
AGENT = "44444444-4444-4444-4444-444444444444"
OWNER = "55555555-5555-5555-5555-555555555555"
OTHER_OWNER = "66666666-6666-6666-6666-666666666666"
REGISTRATION = "77777777-7777-7777-7777-777777777777"
INSTANCE = "control-plane: instance 1"
CARD = "control-plane: card 1"
ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS = [
    "AgentRegistration.Read.All", "AgentRegistration.ReadWrite.All",
    "AgentInstance.ReadWrite.All", "AgentCardManifest.ReadWrite.All",
]


def token(permissions=None, *, delegated=False, **claims):
    payload = {
        "aud": registry.GRAPH_ORIGIN, "tid": TENANT, "oid": CALLER, "appid": CLIENT,
        "scp" if delegated else "roles": " ".join(permissions or []) if delegated else (PERMISSIONS if permissions is None else permissions),
        **claims,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return AccessToken("header." + encoded + ".signature", 5000)


class Response:
    def __init__(self, data=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(data).encode() if raw is None else raw
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        yield self.raw


class RegistryStore:
    """A strict HTTP boundary; PATCH replaces complex fields like the real API.

    Unexpected paths (especially invented GET agentRegistrations collection or
    POST agentCardManifests) fail instead of accidentally accepting a fake API.
    """

    def __init__(self):
        self.records = {}
        self.cards = {}
        self.bindings = {}
        self.calls = []
        self.overrides = {}
        self.post_fault = None

    def request(self, method, url, **options):
        assert options["timeout"] == (5, 20)
        assert options["allow_redirects"] is False
        assert options["stream"] is True
        assert options["headers"]["Authorization"].startswith("Bearer ")
        assert options["headers"]["OData-Version"] == "4.0"
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "graph.microsoft.com"
        path = unquote(parsed.path)
        body = deepcopy(options.get("json"))
        self.calls.append((method, url, body, options["headers"]))
        override = self.overrides.get((method, path, parsed.query), [])
        if override:
            value = override.pop(0)
            if isinstance(value, Exception):
                raise value
            return value(self, body) if callable(value) else value
        if path == registry.INSTANCE_PATH and method == "GET":
            return Response({"value": list(deepcopy(self.records).values())})
        if path in (registry.REGISTRATION_PATH, registry.INSTANCE_PATH) and method == "POST":
            legacy = path == registry.INSTANCE_PATH
            value = INSTANCE if legacy else REGISTRATION
            assert value not in self.records, "Duplicate POST attempted"
            self.records[value] = {"id": value, **body}
            if legacy:
                manifest = self.records[value].pop("agentCardManifest")
                self.cards[CARD] = {"id": CARD, **manifest}
                self.bindings[value] = CARD
            if self.post_fault == "timeout":
                raise registry.requests.Timeout("SECRET transport response")
            if self.post_fault == "conflict":
                return Response({"error": "SECRET remote error"}, status=409)
            return Response({"id": value}, status=201)
        for base in (registry.REGISTRATION_PATH, registry.INSTANCE_PATH):
            if path.startswith(base + "/"):
                suffix = path[len(base) + 1:]
                if suffix.endswith("/agentCardManifest"):
                    assert base == registry.INSTANCE_PATH and method == "GET"
                    value = suffix[:-len("/agentCardManifest")]
                    card_id = self.bindings.get(value)
                    return Response({"value": [deepcopy(self.cards[card_id])] if card_id else []})
                if suffix not in self.records:
                    return Response({"error": "SECRET missing"}, status=404)
                if method == "GET":
                    return Response(deepcopy(self.records[suffix]))
                if method == "PATCH":
                    allowed = {
                        "displayName", "ownerIds", "sourceAgentId", "originatingStore",
                        "agentIdentityId", "agentIdentityBlueprintId",
                    } | ({"url", "preferredTransport", "managedBy", "agentCardManifest"} if base == registry.INSTANCE_PATH else {
                        "description", "managedByAppId", "agentCard", "sourceLastModifiedDateTime",
                    })
                    assert set(body) <= allowed
                    if "agentCardManifest" in body:
                        assert suffix not in self.bindings
                        self.cards[CARD] = {"id": CARD, **body.pop("agentCardManifest")}
                        self.bindings[suffix] = CARD
                    self.records[suffix].update(body)
                    return Response(status=204 if base == registry.INSTANCE_PATH else 200, raw=b"")
        if path.startswith(registry.CARD_PATH + "/") and method == "PATCH":
            value = path[len(registry.CARD_PATH) + 1:]
            assert value in self.cards
            assert not set(body).intersection({"id", "isBlocked", "createdBy", "createdDateTime", "securityProfile"})
            self.cards[value].update(body)
            return Response(deepcopy(self.cards[value]))
        raise AssertionError("Unexpected Graph method/path")

    @property
    def writes(self):
        return [(method, url, body) for method, url, body, _ in self.calls if method != "GET"]


def client(store=None, permissions=None, **token_options):
    store = store or RegistryStore()
    credential = Mock()
    credential.get_token.return_value = token(permissions, **token_options)
    session = Mock()
    session.request.side_effect = store.request
    graph = registry.GraphClient(credential=credential, session=session, tenant_id=TENANT)
    return graph, store


def inputs():
    return (
        {
            "displayName": "Next Best Action", "description": "MCP task planning.",
            "ownerIds": [OWNER], "sourceAgentId": "control-plane:dev:nba",
            "originatingStore": "Azure Agents Control Plane", "agentIdentityId": AGENT,
        },
        {
            "name": "Next Best Action", "description": "MCP task planning, not A2A.",
            "version": "1.0.0", "url": "https://agent.example/mcp",
            "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
            "skills": [{"id": "plan", "name": "Plan", "description": "Plan a task.", "tags": ["mcp"]}],
        },
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in list(cli.os.environ):
        if key.startswith(("AGENT_REGISTRY_", "AGENT_IDENTITY_")) or key in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_AUTHORITY_HOST", "AZURE_ENV_NAME", "OWNER_USER_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(registry.requests.Session, "send", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(registry, "AzureCliCredential", Mock(side_effect=AssertionError("Unexpected Azure CLI")))
    monkeypatch.setattr(registry.time, "time", lambda: 1000)
    monkeypatch.setattr(registry, "_now", lambda: "2026-09-06T00:00:00Z")


def test_current_first_create_uses_documented_beta_schema_and_checkpoints_id(tmp_path):
    graph, store = client()
    instance, card = inputs()
    journal = tmp_path / "registry.json"
    result = registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal)
    assert result == {"agentIdentityId": AGENT, "agentRegistrationId": REGISTRATION}
    assert len(store.writes) == 1
    method, url, body = store.writes[0]
    assert method == "POST" and url == registry.GRAPH_ORIGIN + "/beta/copilot/agentRegistrations"
    assert body == {
        **instance, "agentCard": card, "createdBy": CALLER,
        "sourceCreatedDateTime": "2026-09-06T00:00:00Z",
        "sourceLastModifiedDateTime": "2026-09-06T00:00:00Z",
    }
    assert not set(body).intersection({"id", "url", "managedBy", "agentCardManifest", "isBlocked", "securityProfile"})
    saved = json.loads(journal.read_text())
    assert saved["registryId"] == REGISTRATION and saved["phase"] == "ready"
    assert set(saved) == {"version", "api", "tenantId", "sourceKey", "agentIdentityId", "phase", "registryId"}
    assert graph.credential.get_token.call_args.args == (registry.GRAPH_SCOPE,)
    assert "Bearer" not in journal.read_text() and "https://agent.example" not in journal.read_text()


def test_current_rerun_finds_saved_id_and_is_noop(tmp_path):
    graph, store = client()
    instance, card = inputs()
    journal = tmp_path / "registry.json"
    registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal)
    store.calls.clear()
    result = registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal)
    assert result["agentRegistrationId"] == REGISTRATION
    assert not store.writes
    assert all(urlsplit(url).path != registry.REGISTRATION_PATH for _, url, _, _ in store.calls)


def test_current_update_preserves_unknown_metadata_owners_and_skills(tmp_path):
    graph, store = client()
    instance, card = inputs()
    publisher = registry.AgentRegistryPublisher(graph)
    publisher.publish(instance, card, journal_path=tmp_path / "state.json")
    record = store.records[REGISTRATION]
    record.update(isBlocked=False, unknownAdminMetadata={"keep": True})
    record["ownerIds"] = [OTHER_OWNER, OWNER]
    record["agentCard"]["extensions"] = {"vendor": {"keep": 1}}
    record["agentCard"]["security"] = [{"existingScheme": []}]
    record["agentCard"]["skills"][0]["unknownSkillField"] = "keep"
    record["agentCard"]["skills"].append({"id": "unrelated", "name": "Keep"})
    created_by, created_at = record["createdBy"], record["sourceCreatedDateTime"]
    instance["displayName"] = card["name"] = "Updated name"
    card["url"] = "https://new-agent.example/mcp"
    card["skills"][0]["description"] = "Updated planning."
    store.calls.clear()
    publisher.publish(instance, card, saved_id=REGISTRATION)
    assert len(store.writes) == 1
    patch = store.writes[0][2]
    assert set(patch) == {"displayName", "agentCard", "sourceLastModifiedDateTime"}
    assert patch["agentCard"]["extensions"] == {"vendor": {"keep": 1}}
    assert patch["agentCard"]["security"] == [{"existingScheme": []}]
    assert patch["agentCard"]["skills"][0]["unknownSkillField"] == "keep"
    assert patch["agentCard"]["skills"][1] == {"id": "unrelated", "name": "Keep"}
    assert record["ownerIds"] == [OTHER_OWNER, OWNER]
    assert record["unknownAdminMetadata"] == {"keep": True} and record["isBlocked"] is False
    assert record["createdBy"] == created_by and record["sourceCreatedDateTime"] == created_at
    store.calls.clear()
    publisher.publish(instance, card, saved_id=REGISTRATION)
    assert not store.writes


def test_explicit_saved_id_adopts_precreated_metadata_without_post():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, "agentIdentityId": AGENT, "displayName": "Old", "ownerIds": [OTHER_OWNER]}
    result = registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert result["agentRegistrationId"] == REGISTRATION
    assert len(store.writes) == 1 and store.writes[0][0] == "PATCH"
    assert store.records[REGISTRATION]["sourceAgentId"] == instance["sourceAgentId"]
    assert store.records[REGISTRATION]["ownerIds"] == [OTHER_OWNER, OWNER]


@pytest.mark.parametrize("field,value", [("sourceAgentId", "other-source"), ("originatingStore", "other-store"), ("agentIdentityId", OWNER)])
def test_explicit_id_never_takes_over_another_source_or_identity(field, value):
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": card, field: value}
    with pytest.raises(registry.RegistryError, match="different"):
        registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert not store.writes


def test_current_create_requires_durable_journal_or_adoption():
    graph, store = client()
    with pytest.raises(registry.RegistryError, match="durable"):
        registry.AgentRegistryPublisher(graph).publish(*inputs())
    assert not store.calls


@pytest.mark.parametrize("fault", ["timeout", "conflict"])
def test_current_uncertain_create_never_reposts_and_supports_explicit_recovery(tmp_path, fault):
    graph, store = client()
    store.post_fault = fault
    journal = tmp_path / "state.json"
    publisher = registry.AgentRegistryPublisher(graph)
    with pytest.raises(registry.RegistryError) as failure:
        publisher.publish(*inputs(), journal_path=journal)
    assert "SECRET" not in "".join(traceback.format_exception(failure.value))
    assert json.loads(journal.read_text())["phase"] == "pending"
    assert REGISTRATION in store.records
    before = len(store.calls)
    with pytest.raises(registry.RegistryError, match="unknown"):
        registry.AgentRegistryPublisher(graph).publish(*inputs(), journal_path=journal)
    assert len(store.calls) == before
    store.post_fault = None
    result = registry.AgentRegistryPublisher(graph).publish(*inputs(), saved_id=REGISTRATION, journal_path=journal)
    assert result["agentRegistrationId"] == REGISTRATION
    assert json.loads(journal.read_text())["phase"] == "ready"
    assert sum(method == "POST" for method, _, _ in store.writes) == 1


def test_journal_scope_and_explicit_id_must_match(tmp_path):
    graph, store = client()
    instance, card = inputs()
    journal = tmp_path / "state.json"
    registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal)
    store.calls.clear()
    with pytest.raises(registry.RegistryError, match="differs"):
        registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal, saved_id=OWNER)
    instance["sourceAgentId"] = "changed-stable-key"
    with pytest.raises(registry.RegistryError, match="different tenant"):
        registry.AgentRegistryPublisher(graph).publish(instance, card, journal_path=journal)
    assert not store.calls


@pytest.mark.parametrize("api,saved", [("agent365", REGISTRATION), ("entra-beta", INSTANCE)])
def test_missing_saved_id_never_recreates(api, saved):
    graph, store = client()
    with pytest.raises(registry.RegistryError):
        registry.AgentRegistryPublisher(graph, api=api).publish(*inputs(), saved_id=saved)
    assert not store.writes


@pytest.mark.parametrize("signal", [{"isBlocked": True}, {"isQuarantined": True}, {"status": "Quarantined"}, {"isBlocked": "unknown"}])
@pytest.mark.parametrize("api,value", [("agent365", REGISTRATION), ("entra-beta", INSTANCE)])
def test_publication_never_unblocks_or_patches_quarantined_metadata(signal, api, value):
    graph, store = client()
    instance, card = inputs()
    store.records[value] = {"id": value, **instance, **signal}
    with pytest.raises(registry.RegistryError, match="blocked/quarantined"):
        registry.AgentRegistryPublisher(graph, api=api).publish(instance, card, saved_id=value)
    assert not store.writes


def test_legacy_first_create_registers_supported_card_inline_and_rerun_updates_separately():
    graph, store = client()
    instance, card = inputs()
    publisher = registry.AgentRegistryPublisher(graph, api="entra-beta")
    result = publisher.publish(instance, card)
    assert result == {"agentIdentityId": AGENT, "agentInstanceId": INSTANCE, "agentCardManifestId": CARD}
    assert len(store.writes) == 1
    method, url, body = store.writes[0]
    assert method == "POST" and url == registry.GRAPH_ORIGIN + registry.INSTANCE_PATH
    assert set(body) == {"displayName", "ownerIds", "sourceAgentId", "originatingStore", "agentIdentityId", "url", "preferredTransport", "agentCardManifest"}
    assert body["preferredTransport"] == "JSONRPC"
    manifest = body["agentCardManifest"]
    assert manifest["displayName"] == card["name"] and "name" not in manifest
    assert manifest["skills"][0]["displayName"] == "Plan"
    assert "name" not in manifest["skills"][0] and "url" not in manifest
    assert not set(body).intersection({"isBlocked", "agentIdentity", "securityProfile", "description", "createdBy"})
    store.calls.clear()
    assert publisher.publish(instance, card) == result
    assert not store.writes
    instance["displayName"] = card["name"] = "Updated"
    card["url"] = "https://updated.example/mcp"
    card["version"] = "1.1.0"
    publisher.publish(instance, card)
    assert len(store.writes) == 2
    instance_patch, card_patch = store.writes
    assert instance_patch[2] == {"displayName": "Updated", "url": "https://updated.example/mcp"}
    assert card_patch[2] == {"displayName": "Updated", "version": "1.1.0"}
    assert registry.CARD_PATH in card_patch[1]


def test_legacy_adds_missing_card_using_instance_patch_not_undocumented_post():
    graph, store = client()
    instance, card = inputs()
    store.records[INSTANCE] = {
        "id": INSTANCE, **{k: v for k, v in instance.items() if k != "description"},
        "url": card["url"], "preferredTransport": "JSONRPC",
    }
    result = registry.AgentRegistryPublisher(graph, api="entra-beta").publish(instance, card)
    assert result["agentCardManifestId"] == CARD
    assert len(store.writes) == 1
    assert store.writes[0][0] == "PATCH" and set(store.writes[0][2]) == {"agentCardManifest"}


def test_legacy_find_exhausts_pages_and_fails_ambiguous_before_write():
    graph, store = client()
    instance, card = inputs()
    next_url = registry.GRAPH_ORIGIN + registry.INSTANCE_PATH + "?$skiptoken=page2"
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [{"id": "first", **instance}], "@odata.nextLink": next_url})]
    store.overrides[("GET", registry.INSTANCE_PATH, "$skiptoken=page2")] = [Response({"value": [{"id": "second", **instance}]})]
    with pytest.raises(registry.RegistryError, match="Multiple instances"):
        registry.AgentRegistryPublisher(graph, api="entra-beta").publish(instance, card)
    assert store.calls[-1][1] == next_url
    assert not store.writes


def test_legacy_ignores_display_name_and_matches_both_stable_fields():
    graph, store = client()
    instance, card = inputs()
    store.records["unrelated"] = {"id": "unrelated", **instance, "originatingStore": "Different store"}
    result = registry.AgentRegistryPublisher(graph, api="entra-beta").publish(instance, card)
    assert result["agentInstanceId"] == INSTANCE
    assert len(store.writes) == 1
    assert store.records["unrelated"]["originatingStore"] == "Different store"


@pytest.mark.parametrize("fault", ["conflict", "timeout"])
def test_legacy_create_conflict_or_uncertain_response_reads_and_reconciles_once(fault):
    graph, store = client()
    store.post_fault = fault
    result = registry.AgentRegistryPublisher(graph, api="entra-beta").publish(*inputs())
    assert result["agentInstanceId"] == INSTANCE
    assert len(store.writes) == 1 and store.writes[0][0] == "POST"


def test_legacy_conflict_without_unique_visible_record_fails_without_second_post():
    graph, store = client()
    store.overrides[("POST", registry.INSTANCE_PATH, "")] = [Response({"error": "SECRET"}, status=409)]
    with pytest.raises(registry.RegistryError, match="could not be reconciled"):
        registry.AgentRegistryPublisher(graph, api="entra-beta").publish(*inputs())
    assert len(store.writes) == 1


def test_update_conflict_rereads_and_recomputes_without_erasing_concurrent_owner():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": card, "displayName": "Old", "ownerIds": []}

    def conflict(state, body):
        state.records[REGISTRATION]["ownerIds"] = [OTHER_OWNER]
        state.records[REGISTRATION]["@odata.etag"] = 'W/"new"'
        return Response(status=412)

    store.overrides[("PATCH", registry.REGISTRATION_PATH + "/" + REGISTRATION, "")] = [conflict]
    registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert len(store.writes) == 2
    assert store.writes[1][2]["ownerIds"] == [OTHER_OWNER, OWNER]
    patch_calls = [call for call in store.calls if call[0] == "PATCH"]
    assert patch_calls[1][3]["If-Match"] == 'W/"new"'


def test_update_conflict_reconciled_by_other_writer_becomes_noop():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": card, "displayName": "Old"}

    def conflict(state, body):
        state.records[REGISTRATION].update(body)
        return Response(status=409)

    store.overrides[("PATCH", registry.REGISTRATION_PATH + "/" + REGISTRATION, "")] = [conflict]
    registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert len(store.writes) == 1


def test_update_conflicts_are_bounded():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": card, "displayName": "Old"}
    store.overrides[("PATCH", registry.REGISTRATION_PATH + "/" + REGISTRATION, "")] = [Response(status=412)] * 3
    with pytest.raises(registry.RegistryError):
        registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert len(store.writes) == 3


def test_legacy_card_collection_ambiguity_blocks_update():
    graph, store = client()
    instance, card = inputs()
    publisher = registry.AgentRegistryPublisher(graph, api="entra-beta")
    publisher.publish(instance, card)
    store.calls.clear()
    path = registry.INSTANCE_PATH + "/" + INSTANCE + "/agentCardManifest"
    store.overrides[("GET", path, "")] = [Response({"value": [{"id": "one"}, {"id": "two"}]})]
    with pytest.raises(registry.RegistryError, match="multiple card"):
        publisher.publish(instance, card)
    assert not store.writes


@pytest.mark.parametrize("next_url", [
    "https://graph.microsoft.com.evil.example/beta/agentRegistry/agentInstances",
    "https://graph.microsoft.com@evil.example/beta/agentRegistry/agentInstances",
    "https://user@graph.microsoft.com/beta/agentRegistry/agentInstances",
    "https://graph.microsoft.com:444/beta/agentRegistry/agentInstances",
    "http://graph.microsoft.com/beta/agentRegistry/agentInstances",
    "//evil.example/beta/agentRegistry/agentInstances",
    "/beta/agentRegistry/agentInstances?page=2",
    "https://graph.microsoft.com/beta/users",
    "https://graph.microsoft.com/v1.0/agentRegistry/agentInstances",
    "https://graph.microsoft.com/beta/agentRegistry/../users",
    "https://graph.microsoft.com/beta/agentRegistry/%2e%2e/users",
    "https://graph.microsoft.com/beta/agentRegistry/agentInstances#SECRET",
    "https://graph.microsoft.com\\@evil.example/beta/agentRegistry/agentInstances",
])
def test_pagination_refuses_credential_exfiltration_and_collection_changes(next_url):
    graph, store = client()
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [], "@odata.nextLink": next_url})]
    with pytest.raises(registry.RegistryError) as failure:
        graph.collection(registry.INSTANCE_PATH)
    assert "SECRET" not in str(failure.value)
    assert len(store.calls) == 1


def test_pagination_same_origin_is_supported_and_loop_is_rejected():
    graph, store = client()
    next_url = registry.GRAPH_ORIGIN + registry.INSTANCE_PATH + "?$skiptoken=two"
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [{"id": "a"}], "@odata.nextLink": next_url})]
    store.overrides[("GET", registry.INSTANCE_PATH, "$skiptoken=two")] = [Response({"value": [{"id": "b"}]})]
    assert graph.collection(registry.INSTANCE_PATH) == [{"id": "a"}, {"id": "b"}]
    store.calls.clear()
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [], "@odata.nextLink": registry.GRAPH_ORIGIN + registry.INSTANCE_PATH})]
    with pytest.raises(registry.RegistryError, match="pagination"):
        graph.collection(registry.INSTANCE_PATH)
    assert len(store.calls) == 1


def test_pagination_page_and_item_limits(monkeypatch):
    graph, store = client()
    monkeypatch.setattr(registry, "_MAX_PAGES", 1)
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [], "@odata.nextLink": registry.GRAPH_ORIGIN + registry.INSTANCE_PATH + "?page=2"})]
    with pytest.raises(registry.RegistryError, match="page limit"):
        graph.collection(registry.INSTANCE_PATH)
    assert len(store.calls) == 1
    monkeypatch.setattr(registry, "_MAX_ITEMS", 1)
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response({"value": [{}, {}]})]
    with pytest.raises(registry.RegistryError, match="item limit"):
        graph.collection(registry.INSTANCE_PATH)


@pytest.mark.parametrize("body", [{}, {"value": None}, {"value": [None]}, {"id": "singleton"}])
def test_malformed_collection_is_not_an_empty_registry(body):
    graph, store = client()
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [Response(body)]
    with pytest.raises(registry.RegistryError, match="collection"):
        graph.collection(registry.INSTANCE_PATH)


def test_documented_singleton_relationship_shape_also_supported():
    graph, store = client()
    path = registry.INSTANCE_PATH + "/id/agentCardManifest"
    store.overrides[("GET", path, "")] = [Response({"id": CARD})]
    assert graph.collection(path, allow_singleton=True) == [{"id": CARD}]


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_http_errors_are_secret_safe_and_never_redirect_or_retry(status):
    graph, store = client()
    response = Response(status=status, raw=b"SECRET token or signed callback URL")
    store.overrides[("GET", registry.INSTANCE_PATH, "")] = [response]
    with pytest.raises(registry.RegistryError) as failure:
        graph.request("GET", registry.INSTANCE_PATH)
    assert failure.value.http_status == status
    assert "SECRET" not in "".join(traceback.format_exception(failure.value))
    assert response.closed and len(store.calls) == 1


def test_transport_malformed_and_oversized_response_do_not_echo_body(monkeypatch):
    graph, store = client()
    for response in [registry.requests.Timeout("SECRET transport"), Response(raw=b"SECRET invalid JSON"), Response(raw=b"x" * 32)]:
        monkeypatch.setattr(registry, "_MAX_RESPONSE_BYTES", 16)
        store.overrides[("GET", registry.INSTANCE_PATH, "")] = [response]
        with pytest.raises(registry.RegistryError) as failure:
            graph.request("GET", registry.INSTANCE_PATH)
        assert "SECRET" not in "".join(traceback.format_exception(failure.value))


def test_read_deadline_is_bounded(monkeypatch):
    graph, store = client()
    ticks = iter([0, 31])
    monkeypatch.setattr(registry.time, "monotonic", lambda: next(ticks))
    with pytest.raises(registry.RegistryError, match="time limit"):
        graph.request("GET", registry.INSTANCE_PATH)


def test_permission_preflight_requires_write_consent_not_just_successful_reads():
    graph, store = client(permissions=["AgentRegistration.Read.All"])
    report = registry.AgentRegistryPublisher(graph).preflight()
    assert report["status"] == "missing_permissions"
    assert report["missingPermissions"] == ["AgentRegistration.ReadWrite.All"]
    assert not store.calls


def test_opaque_graph_token_is_inconclusive_not_success():
    graph, store = client()
    graph.credential.get_token.return_value = AccessToken("opaque-SECRET", 5000)
    report = registry.AgentRegistryPublisher(graph).preflight()
    assert report["status"] == "inconclusive" and "SECRET" not in json.dumps(report)
    assert not store.calls


@pytest.mark.parametrize("delegated,manager,expected", [(False, CLIENT, "satisfied"), (False, None, "missing_permissions"), (False, OWNER, "missing_permissions"), (True, CLIENT, "missing_permissions")])
def test_managedby_is_only_valid_for_matching_app_only_publisher(delegated, manager, expected):
    graph, _ = client(permissions=["AgentInstance.ReadWrite.ManagedBy", "AgentCardManifest.ReadWrite.ManagedBy"], delegated=delegated)
    report = registry.AgentRegistryPublisher(graph, api="entra-beta").preflight(managed_by=manager)
    assert report["status"] == expected


def test_directory_permission_error_in_publisher_preflight_is_inconclusive():
    graph, store = client()
    store.overrides[("GET", registry.INSTANCE_PATH, "$select=id")] = [Response(status=403, raw=b"SECRET")]
    report = registry.AgentRegistryPublisher(graph, api="entra-beta").preflight()
    assert report["status"] == "inconclusive" and report["error"]["httpStatus"] == 403
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("field", ["securityProfile", "approvalConfiguration", "isBlocked", "id", "agentIdentity", "$schema"])
def test_unsupported_registration_fields_are_rejected_before_http(field):
    graph, store = client()
    instance, card = inputs()
    instance[field] = "SECRET unsupported"
    with pytest.raises(registry.RegistryError) as failure:
        registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert "SECRET" not in str(failure.value)
    assert not store.calls and graph.credential.get_token.call_count == 0


@pytest.mark.parametrize("url", ["http://agent.example", "https://user:SECRET@agent.example", "https://agent.example/mcp?sig=SECRET", "https://agent.example/#SECRET", "${AGENT_ENDPOINT_URL}", "https://agent.example\\@evil.example"])
def test_endpoint_must_not_contain_credentials_or_unresolved_placeholders(url):
    instance, card = inputs()
    card["url"] = url
    with pytest.raises(registry.RegistryError) as failure:
        registry.validate_publication(instance, card)
    assert "SECRET" not in str(failure.value)


def test_skill_schema_rejects_inputschema_and_duplicate_ids():
    instance, card = inputs()
    card["skills"][0]["inputSchema"] = {"type": "object"}
    with pytest.raises(registry.RegistryError, match="skill fields"):
        registry.validate_publication(instance, card)
    del card["skills"][0]["inputSchema"]
    card["skills"].append(deepcopy(card["skills"][0]))
    with pytest.raises(registry.RegistryError, match="unique"):
        registry.validate_publication(instance, card)


def test_unknown_existing_card_shape_does_not_get_overwritten():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": {**card, "skills": [{"name": "No stable ID"}]}}
    with pytest.raises(registry.RegistryError, match="safely by ID"):
        registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert not store.writes


@pytest.mark.parametrize("flag", [None, "false", "FALSE", "0"])
def test_cli_disabled_even_with_publish_never_authenticates(flag, monkeypatch, capsys):
    if flag is not None:
        monkeypatch.setenv("AGENT_REGISTRY_ENABLED", flag)
    assert cli.main(["--publish"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "skipped"
    registry.AzureCliCredential.assert_not_called()


def test_cli_invalid_flag_and_arguments_do_not_echo_values(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_REGISTRY_ENABLED", "SECRET misconfiguration")
    assert cli.main([]) == 2
    assert "SECRET" not in capsys.readouterr().out
    assert cli.main(["--unknown", "SECRET argument"]) == 2
    assert "SECRET" not in capsys.readouterr().out
    registry.AzureCliCredential.assert_not_called()


def test_cli_preflight_needs_no_deployed_endpoint_identity_or_flag(monkeypatch, capsys):
    graph, store = client()
    monkeypatch.setattr(cli, "GraphClient", lambda **kwargs: graph)
    assert cli.main(["--preflight"]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "satisfied" and report["endpointCheck"] == "notAvailableWithoutSavedId"
    assert not store.calls


def test_cli_env_contract_templates_and_success_output_only_ids(tmp_path, monkeypatch, capsys):
    graph, store = client()
    monkeypatch.setattr(cli, "GraphClient", lambda **kwargs: graph)
    for key, value in {
        "AGENT_REGISTRY_ENABLED": "true", "AGENT_IDENTITY_APP_ID": AGENT,
        "AGENT_IDENTITY_DISPLAY_NAME": "Name with \"quotes\"", "AGENT_ENDPOINT_URL": "https://agent.example/mcp",
        "AGENT_REGISTRY_OWNER_IDS": OWNER + "," + OTHER_OWNER, "AZURE_ENV_NAME": "unit",
        "AGENT_REGISTRY_STATE_FILE": str(tmp_path / "journal.json"),
    }.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["--publish"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"agentIdentityId": AGENT, "agentRegistrationId": REGISTRATION}
    payload = store.writes[0][2]
    assert payload["sourceAgentId"] == "azure-agents-control-plane:unit:next-best-action"
    assert payload["displayName"] == 'Name with "quotes"'
    assert payload["ownerIds"] == [OWNER, OTHER_OWNER]
    assert payload["originatingStore"] == "Azure Agents Control Plane"
    assert "securityProfile" not in json.dumps(payload)
    assert "supportsA2A" not in json.dumps(payload)
    store.calls.clear()
    assert cli.main([]) == 0
    capsys.readouterr()
    assert not store.writes


def test_default_transport_uses_only_azure_cli_not_runtime_identity(monkeypatch):
    deployment = Mock()
    factory = Mock(return_value=deployment)
    monkeypatch.setattr(registry, "AzureCliCredential", factory)
    monkeypatch.setenv("AGENT_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("AGENT_IDENTITY_APP_ID", AGENT)
    monkeypatch.setenv("AZURE_CLIENT_ID", AGENT)
    with registry.GraphClient(tenant_id=TENANT):
        factory.assert_called_once_with(tenant_id=TENANT, process_timeout=10)
    deployment.close.assert_called_once()


def test_default_transport_rejects_nonpublic_authority_before_auth(monkeypatch):
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.us")
    with pytest.raises(registry.RegistryError, match="public cloud"):
        registry.GraphClient()
    registry.AzureCliCredential.assert_not_called()


def test_authentication_error_suppresses_credential_details():
    graph, store = client()
    graph.credential.get_token.side_effect = RuntimeError("SECRET credential diagnostic")
    with pytest.raises(registry.RegistryError) as failure:
        graph.request("GET", registry.INSTANCE_PATH)
    assert "SECRET" not in "".join(traceback.format_exception(failure.value))
    assert not store.calls


def test_read_only_and_secret_governance_fields_absent_from_reference_templates():
    instance = json.loads((ROOT / "agent365/manifests/agent_instance.json").read_text())
    card = json.loads((ROOT / "agent365/manifests/agent_card_manifest.json").read_text())
    assert set(instance) <= registry._INSTANCE_FIELDS
    assert set(card) <= registry._CARD_FIELDS
    combined = json.dumps([instance, card])
    for forbidden in ("securityProfile", "isBlocked", "approvalConfiguration", "securityRequirements", "supportsA2A", "inputSchema", "${MANAGED_IDENTITY_CLIENT_ID}", "${TENANT_ID}", "$schema"):
        assert forbidden not in combined


def test_registry_ids_follow_string_schema_not_assumed_guid():
    graph, store = client()
    instance, card = inputs()
    opaque_id = "registration: opaque-id"
    store.records[opaque_id] = {"id": opaque_id, **instance, "agentCard": card}
    result = registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=opaque_id)
    assert result["agentRegistrationId"] == opaque_id and not store.writes


def test_explicit_source_modified_timestamp_is_honored_without_rewriting_creation():
    graph, store = client()
    instance, card = inputs()
    store.records[REGISTRATION] = {
        "id": REGISTRATION, **instance, "agentCard": card, "createdBy": CALLER,
        "sourceCreatedDateTime": "2026-01-01T00:00:00Z", "sourceLastModifiedDateTime": "2026-01-01T00:00:00Z",
    }
    instance["sourceLastModifiedDateTime"] = "2026-09-01T12:00:00Z"
    publisher = registry.AgentRegistryPublisher(graph)
    publisher.publish(instance, card, saved_id=REGISTRATION)
    assert store.writes[0][2] == {"sourceLastModifiedDateTime": "2026-09-01T12:00:00Z"}
    assert store.records[REGISTRATION]["sourceCreatedDateTime"] == "2026-01-01T00:00:00Z"
    store.calls.clear()
    publisher.publish(instance, card, saved_id=REGISTRATION)
    assert not store.writes


def test_missing_publication_consent_stops_before_journal_reservation_or_post(tmp_path):
    graph, store = client(permissions=["AgentRegistration.Read.All"])
    journal = tmp_path / "state.json"
    with pytest.raises(registry.RegistryError, match="AgentRegistration.ReadWrite.All"):
        registry.AgentRegistryPublisher(graph).publish(*inputs(), journal_path=journal)
    assert not journal.exists() and not store.calls


def test_server_denied_publication_never_falls_back_to_other_api_or_credential(tmp_path):
    graph, store = client()
    store.overrides[("POST", registry.REGISTRATION_PATH, "")] = [Response(status=403, raw=b"SECRET denied")]
    journal = tmp_path / "state.json"
    with pytest.raises(registry.RegistryError) as failure:
        registry.AgentRegistryPublisher(graph).publish(*inputs(), journal_path=journal)
    assert failure.value.http_status == 403
    assert failure.value.code == "permission_denied"
    assert "SECRET" not in str(failure.value)
    assert len(store.calls) == 1 and store.calls[0][0] == "POST"
    assert not journal.exists()  # A later invocation after consent may try creation.
    registry.AzureCliCredential.assert_not_called()


@pytest.mark.parametrize("status", [403, 404])
def test_unreadable_card_is_not_interpreted_as_missing_and_overwritten(status):
    graph, store = client()
    publisher = registry.AgentRegistryPublisher(graph, api="entra-beta")
    publisher.publish(*inputs())
    store.calls.clear()
    path = registry.INSTANCE_PATH + "/" + INSTANCE + "/agentCardManifest"
    store.overrides[("GET", path, "")] = [Response(status=status, raw=b"SECRET hidden card")]
    with pytest.raises(registry.RegistryError) as failure:
        publisher.publish(*inputs())
    assert failure.value.http_status == status and not store.writes


def test_runtime_principal_is_rejected_even_if_misgranted_publication_roles():
    graph, store = client(appid=AGENT)
    with pytest.raises(registry.RegistryError, match="separate deployment"):
        registry.AgentRegistryPublisher(graph).publish(*inputs(), saved_id=REGISTRATION)
    assert not store.calls


def test_checkpoint_failure_preserves_returned_id_for_explicit_recovery(tmp_path, monkeypatch):
    graph, store = client()
    publisher = registry.AgentRegistryPublisher(graph)
    monkeypatch.setattr(registry.PublicationJournal, "checkpoint", Mock(side_effect=registry.RegistryError("journal_unavailable", "Cannot checkpoint registration ID.")))
    with pytest.raises(registry.RegistryError, match="checkpoint"):
        publisher.publish(*inputs(), journal_path=tmp_path / "state.json")
    assert publisher.published_id == REGISTRATION
    assert len(store.writes) == 1


def test_equivalent_graph_timestamp_format_is_noop():
    graph, store = client()
    instance, card = inputs()
    instance["sourceLastModifiedDateTime"] = "2026-09-06T02:00:00+02:00"
    store.records[REGISTRATION] = {"id": REGISTRATION, **instance, "agentCard": card, "sourceLastModifiedDateTime": "2026-09-06T00:00:00.0000000Z"}
    registry.AgentRegistryPublisher(graph).publish(instance, card, saved_id=REGISTRATION)
    assert not store.writes