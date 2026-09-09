"""Offline callback API tests with REAL RSA-signed JWTs and real JWT verification.

Only JWKS HTTP, Cosmos, and Logic App dispatch are faked. No Azure credentials,
network requests, app/main imports, or subprocesses are used. Runtime packages
plus httpx (FastAPI TestClient) are required; no pytest-asyncio plugin is needed.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

import jwt
import jwt.jwks_client as jwks_module
from azure.core import MatchConditions
from azure.cosmos import exceptions as cosmos_exceptions
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent365_approval as approvals
from src import approval_api as api


TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
PRINCIPAL = "33333333-3333-3333-3333-333333333333"
APPROVER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OUTSIDER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AUDIENCE = f"api://{BLUEPRINT}"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
ROUTE = "/approvals/callback"
CONFIG = {
    "AZURE_TENANT_ID": TENANT,
    "APPROVAL_APPROVER_TENANT_ID": TENANT,
    "APPROVAL_CALLBACK_AUDIENCE": AUDIENCE,
    "APPROVAL_CALLBACK_PRINCIPAL_ID": PRINCIPAL,
    "APPROVAL_APPROVER_IDS": APPROVER,
    "APPROVAL_TIMEOUT_HOURS": "2",
    "LOGIC_APP_APPROVAL_WEBHOOK": "https://workflow.example/triggers/manual/invoke?sig=fake-secret",
    "APPROVAL_CALLBACK_URL": "https://gateway.example/mcp/approvals/callback",
    "COSMOSDB_ENDPOINT": "https://cosmos.example/",
    "COSMOSDB_DATABASE_NAME": "mcpdb",
    "COSMOSDB_APPROVALS_CONTAINER": "approvals",
}
CONTEXT = {
    "task": "Deploy API to Kubernetes", "requested_by": "requester", "environment": "production",
    "cluster": "aks-cluster", "namespace": "api", "image_tags": ["api:1"],
    "commit_sha": "123abc", "pipeline_url": "https://pipeline.example/run/1",
    "rollback_url": "https://pipeline.example/rollback/1",
}


class CallbackStore:
    """Minimal partition/CAS fake local to this standalone test module."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.version = 0
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.after_write_error: Exception | None = None

    def _save(self, body: dict[str, Any]) -> dict[str, Any]:
        self.version += 1
        doc = copy.deepcopy(body)
        doc["_etag"] = str(self.version)
        self.docs[(body["environment"], body["id"])] = doc
        return copy.deepcopy(doc)

    def create_item(self, *, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._save(body)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        with self.lock:
            if self.read_error:
                raise self.read_error
            if (partition_key, item) not in self.docs:
                raise cosmos_exceptions.CosmosResourceNotFoundError(status_code=404, message="Not found")
            return copy.deepcopy(self.docs[(partition_key, item)])

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: MatchConditions,
    ) -> dict[str, Any]:
        with self.lock:
            assert match_condition == MatchConditions.IfNotModified
            assert body["partitionKey"] == body["environment"]
            assert body["id"] == item and "_etag" not in body
            if self.write_error:
                raise self.write_error
            if self.docs[(body["environment"], item)]["_etag"] != etag:
                raise cosmos_exceptions.CosmosHttpResponseError(status_code=412, message="Conflict")
            result = self._save(body)
            if self.after_write_error:
                error, self.after_write_error = self.after_write_error, None
                raise error
            return result


class ApprovalCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key())),
            "kid": "unit-signing-key", "use": "sig", "alg": "RS256",
        }

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
        api._get_jwks_client.cache_clear()
        self.addCleanup(api._get_jwks_client.cache_clear)

        def jwks_response(request: Any, **options: Any) -> io.BytesIO:
            self.assertEqual(request.full_url, JWKS_URL)
            self.assertEqual(options["timeout"], 5)
            return io.BytesIO(json.dumps({"keys": [self.jwk]}).encode())

        # Exercise the actual PyJWKClient parsing/cache and RSA verification.
        # Only its HTTP boundary is mocked, never jwt.decode or the signing key.
        opener = patch.object(jwks_module.urllib.request, "urlopen", side_effect=jwks_response)
        self.urlopen = opener.start()
        self.addCleanup(opener.stop)
        self.store = CallbackStore()
        self.now = datetime.now(timezone.utc)
        self.transport = SimpleNamespace(send=AsyncMock())
        self.engine = approvals.ApprovalWorkflowEngine(
            container_client=self.store, transport=self.transport, clock=lambda: self.now,
        )
        self.contract = asyncio.run(self.engine.initiate_approval(**CONTEXT))
        self.body = self.body_for(self.contract)
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.app.dependency_overrides[api.get_approval_workflow_engine] = lambda: self.engine
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def body_for(self, contract: approvals.ApprovalContract, **changes: Any) -> dict[str, Any]:
        return {
            "approval_id": contract.approval_id, "environment": contract.environment,
            "request_hash": contract.request_hash, "decision": "approve",
            "approved_by": APPROVER, "approver_tenant_id": TENANT,
            "comment": "Reviewed", "workflow_run_id": "085-unit-workflow-run",
            **changes,
        }

    def token(
        self, changes: dict[str, Any] | None = None, *, remove: tuple[str, ...] = (),
        key: Any = None, algorithm: str = "RS256", headers: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": AUDIENCE, "tid": TENANT, "oid": PRINCIPAL,
            "ver": "2.0", "idtyp": "app", "iat": now - 60, "nbf": now - 60,
            "exp": now + 3600, **(changes or {}),
        }
        for name in remove:
            claims.pop(name, None)
        return jwt.encode(
            claims, self.private_key if key is None else key, algorithm=algorithm,
            headers={"kid": "unit-signing-key", **(headers or {})},
        )

    def post(self, body: Any = None, *, token: str | None = None) -> Any:
        return self.client.post(
            ROUTE, json=self.body if body is None else body,
            headers={"Authorization": f"Bearer {self.token() if token is None else token}"},
        )

    def stored(self) -> dict[str, Any]:
        return self.store.docs[(self.contract.environment, self.contract.approval_id)]

    def test_real_rs256_callback_returns_200_only_after_persistence(self) -> None:
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["decision"], "approved")
        self.assertEqual(response.json()["agent_validation"], "passed")
        self.assertEqual(self.stored()["decision"], "approved")
        self.assertEqual(self.stored()["approved_by"], APPROVER)
        self.assertIn("response_hash", self.stored())
        self.assertNotIn("_etag", response.json())
        self.assertNotIn("fake-secret", response.text)
        self.urlopen.assert_called_once()

    def test_v2_accepts_resource_uri_and_blueprint_uuid_audiences(self) -> None:
        for audience in (AUDIENCE, BLUEPRINT):
            response = self.post(token=self.token({"aud": audience}))
            self.assertEqual(response.status_code, 200, response.text)
        self.urlopen.assert_called_once()  # Actual PyJWKClient JWKS cache.

    def test_v1_managed_identity_issuer_and_resource_uri(self) -> None:
        response = self.post(token=self.token({
            "ver": "1.0", "iss": f"https://sts.windows.net/{TENANT}/", "aud": AUDIENCE,
        }))
        self.assertEqual(response.status_code, 200, response.text)
        response = self.post(token=self.token({
            "ver": "1.0", "iss": f"https://sts.windows.net/{TENANT}/", "aud": BLUEPRINT,
        }))
        self.assertEqual(response.status_code, 401)

    def test_unknown_signing_key_and_wrong_rsa_signature_are_401(self) -> None:
        for token in (self.token(headers={"kid": "unknown-key"}), self.token(key=self.wrong_key)):
            response = self.post(token=token)
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            self.assertNotIn(token, response.text)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_unsigned_hmac_and_untrusted_jwks_header_are_rejected_before_network(self) -> None:
        for token in (
            self.token(key="", algorithm="none"),
            self.token(key="not-an-rsa-key-" * 8, algorithm="HS256"),
            self.token(headers={"jku": "https://attacker.example/keys"}),
            self.token(headers={"x5u": "https://attacker.example/certificate"}),
            self.token(headers={"crit": ["unsupported"]}),
        ):
            self.assertEqual(self.post(token=token).status_code, 401)
        self.urlopen.assert_not_called()

    def test_expiry_not_before_issuer_tenant_and_audience_are_verified(self) -> None:
        for changes in (
            {"exp": int(time.time()) - 1}, {"nbf": int(time.time()) + 3600},
            {"iat": int(time.time()) + 3600}, {"tid": OUTSIDER},
            {"iss": "https://attacker.example/"}, {"aud": f"api://{OUTSIDER}"},
            {"iss": f"https://login.microsoftonline.com/{OUTSIDER}/v2.0"},
            {"ver": "2.0", "iss": f"https://sts.windows.net/{TENANT}/"},
            {"ver": "3.0"}, {"aud": [AUDIENCE]}, {"exp": str(int(time.time()) + 3600)},
        ):
            with self.subTest(changes=changes):
                response = self.post(token=self.token(changes))
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.stored()["decision"], "pending")
        self.assertTrue(all(call.args[0].full_url == JWKS_URL for call in self.urlopen.call_args_list))

    def test_required_signed_claims_cannot_be_omitted(self) -> None:
        for name in ("exp", "iss", "aud", "tid", "oid", "ver"):
            with self.subTest(name=name):
                self.assertEqual(self.post(token=self.token(remove=(name,))).status_code, 401)

    def test_malformed_jwt_payload_and_nonnumeric_exp_are_401(self) -> None:
        header = self.token().split(".")[0]
        self.assertEqual(self.post(token=f"{header}.bm90LWpzb24.c2ln").status_code, 401)
        self.urlopen.assert_not_called()
        for expiry in ({"invalid": 1}, [], "not-a-time"):
            self.assertEqual(self.post(token=self.token({"exp": expiry})).status_code, 401)

    def test_only_configured_oid_is_authorized_not_appid_or_azp(self) -> None:
        for changes in (
            {"oid": OUTSIDER}, {"oid": APPROVER},
            {"oid": OUTSIDER, "appid": PRINCIPAL, "azp": PRINCIPAL},
        ):
            response = self.post(token=self.token(changes))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(token=self.token({"appid": PRINCIPAL}, remove=("oid",))).status_code, 401)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_delegated_tokens_are_forbidden_even_with_correct_oid(self) -> None:
        for changes in ({"scp": "Approval.Write"}, {"scp": ""}, {"idtyp": "user"}):
            self.assertEqual(self.post(token=self.token(changes)).status_code, 403)

    def test_cross_tenant_human_does_not_relax_callback_jwt_tenant(self) -> None:
        with patch.dict(os.environ, {"APPROVAL_APPROVER_TENANT_ID": OUTSIDER}):
            contract = asyncio.run(self.engine.initiate_approval(**CONTEXT))
            body = self.body_for(contract, approver_tenant_id=OUTSIDER)
            self.assertEqual(self.post(body, token=self.token({"tid": OUTSIDER})).status_code, 401)
            self.assertEqual(self.post({**body, "approver_tenant_id": TENANT}).status_code, 403)
            response = self.post(body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["approval_tenant_id"], OUTSIDER)
            self.assertEqual(response.json()["decision"], "approved")

    def test_x_headers_never_authenticate_or_override_a_signed_identity(self) -> None:
        headers = {
            "x-ms-client-principal-id": PRINCIPAL, "x-ms-client-principal": "unsigned",
            "x-approver-id": APPROVER, "x-tenant-id": TENANT,
        }
        response = self.client.post(ROUTE, json=self.body, headers=headers)
        self.assertEqual(response.status_code, 401)
        response = self.client.post(ROUTE, json=self.body, headers={
            **headers, "Authorization": f"Bearer {self.token({'oid': OUTSIDER})}",
        })
        self.assertEqual(response.status_code, 403)

    def test_missing_malformed_oversized_and_duplicate_authorization(self) -> None:
        for authorization in ("", "Basic credentials", "Bearer", "Bearer a b", "Bearer malformed", "Bearer " + "a" * (api.MAX_BEARER_LENGTH + 1)):
            response = self.client.post(ROUTE, json=self.body, headers={"Authorization": authorization})
            self.assertEqual(response.status_code, 401)
        response = self.client.post(ROUTE, json=self.body, headers=[
            ("Authorization", f"Bearer {self.token()}"), ("Authorization", f"Bearer {self.token()}"),
        ])
        self.assertEqual(response.status_code, 401)

    def test_jwks_timeout_or_invalid_document_returns_secret_safe_503(self) -> None:
        self.urlopen.side_effect = URLError("fake-secret in provider diagnostics")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("fake-secret", response.text)
        self.urlopen.side_effect = lambda *args, **kwargs: io.BytesIO(b"not-json fake-secret")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("fake-secret", response.text)

    def test_missing_auth_configuration_is_503_not_an_auth_bypass(self) -> None:
        for name in ("AZURE_TENANT_ID", "APPROVAL_CALLBACK_AUDIENCE", "APPROVAL_CALLBACK_PRINCIPAL_ID"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "fake-secret-invalid"}):
                response = self.post()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("fake-secret", response.text)
        self.urlopen.assert_not_called()

    def test_normalized_duplicates_return_same_result_and_changed_decision_is_409(self) -> None:
        first = self.post()
        etag = self.stored()["_etag"]
        second = self.post({**self.body, "decision": "APPROVED", "approved_by": APPROVER.upper()})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.stored()["_etag"], etag)
        for changes in ({"decision": "reject"}, {"comment": "Different"}, {"workflow_run_id": "different"}):
            self.assertEqual(self.post({**self.body, **changes}).status_code, 409)
        self.assertEqual(self.stored()["_etag"], etag)

    def test_decision_aliases_rejection_timeout_and_error_are_terminal(self) -> None:
        for decision, expected in (("reject", "rejected"), ("REJECTED", "rejected"), ("timeout", "timeout"), ("ERROR", "error")):
            contract = asyncio.run(self.engine.initiate_approval(**CONTEXT))
            system = expected in ("timeout", "error")
            body = self.body_for(
                contract, decision=decision, approved_by="" if system else APPROVER,
                approver_tenant_id=None if system else TENANT,
            )
            response = self.post(body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["decision"], expected)
            self.assertEqual(response.json()["agent_validation"], "failed" if system else "passed")

    def test_invalid_decision_is_400_not_422_or_success(self) -> None:
        for decision in ("pending", "yes", "allow", "", None, True):
            self.assertEqual(self.post({**self.body, "decision": decision}).status_code, 400)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_system_callback_can_omit_human_identity_or_use_empty_connector_fields(self) -> None:
        body = {**self.body, "decision": "timeout", "approved_by": "", "approver_tenant_id": ""}
        first = self.post(body)
        self.assertEqual(first.status_code, 200, first.text)
        body.pop("approved_by")
        body.pop("approver_tenant_id")
        duplicate = self.post(body)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(first.json(), duplicate.json())

    def test_unauthorized_approver_tenant_and_display_name_are_rejected(self) -> None:
        for changes in (
            {"approved_by": OUTSIDER}, {"approved_by": "Jane Admin"}, {"approved_by": "system"},
            {"approved_by": ""}, {"approved_by": None}, {"approver_tenant_id": OUTSIDER},
            {"approver_tenant_id": None}, {"decision": "timeout", "approved_by": APPROVER},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.post({**self.body, **changes}).status_code, 403)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_unknown_id_wrong_environment_and_wrong_hash(self) -> None:
        self.assertEqual(self.post({**self.body, "approval_id": OUTSIDER}).status_code, 404)
        self.assertEqual(self.post({**self.body, "environment": "other-environment"}).status_code, 404)
        self.assertEqual(self.post({**self.body, "request_hash": "0" * 64}).status_code, 409)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_late_callback_cannot_use_an_earlier_timestamp_to_approve(self) -> None:
        timestamp = self.now.isoformat()
        self.now += timedelta(hours=2)
        response = self.post({**self.body, "timestamp": timestamp})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stored()["decision"], "timeout")
        response = self.post({**self.body, "decision": "timeout", "approved_by": "system", "approver_tenant_id": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["agent_validation"], "failed")

    def test_approved_callback_redelivery_after_expiry_does_not_renew_resume(self) -> None:
        first = self.post()
        self.assertEqual(first.status_code, 200)
        self.now += timedelta(hours=3)
        second = self.post()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        with self.assertRaises(approvals.ApprovalConflictError):
            asyncio.run(self.engine.resume_approval(self.contract.approval_id, **CONTEXT))

    def test_storage_failure_is_503_and_no_unpersisted_success_escapes(self) -> None:
        for name in ("read_error", "write_error"):
            setattr(self.store, name, RuntimeError("fake-secret in Cosmos diagnostics"))
            response = self.post()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("fake-secret", response.text)
            self.assertEqual(self.stored()["decision"], "pending")
            setattr(self.store, name, None)
        self.assertEqual(self.post().status_code, 200)

    def test_unknown_write_outcome_is_503_then_reconciles_from_cosmos(self) -> None:
        self.store.after_write_error = TimeoutError("fake-secret")
        first = self.post()
        self.assertEqual(first.status_code, 503)
        etag = self.stored()["_etag"]
        second = self.post()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.stored()["_etag"], etag)

    def test_strict_schema_bounds_types_and_routing_overrides(self) -> None:
        for changes in (
            {"callback_url": "https://attacker.example/?secret=do-not-echo"},
            {"approvers": [OUTSIDER]}, {"displayName": "do-not-echo"}, {"task": "Changed task"},
            {"environment": 123}, {"environment": "x" * 129}, {"environment": "prod/other"},
            {"approved_by": {"id": APPROVER}}, {"request_hash": "not-a-hash"},
            {"comment": "do-not-echo" + "x" * 4096}, {"workflow_run_id": "x" * 513},
            {"workflow_run_id": "  "}, {"timestamp": "x" * 65}, {"timestamp": 123},
            {"timestamp": "2026-09-06T12:00:00"},
        ):
            with self.subTest(keys=list(changes)):
                response = self.post({**self.body, **changes})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn("do-not-echo", response.text)
        for missing in ("approval_id", "environment", "request_hash", "workflow_run_id", "decision"):
            body = dict(self.body)
            body.pop(missing)
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.stored()["decision"], "pending")
        self.transport.send.assert_awaited_once()

    def test_duplicate_json_keys_nonfinite_values_and_invalid_json_are_400(self) -> None:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        for content in (
            json.dumps(self.body)[:-1] + ', "decision": "reject"}',
            json.dumps(self.body)[:-1] + ', "comment": NaN}',
            '{"secret":"do-not-echo"', "null", "[]", "[" * 2000, b"\xff",
        ):
            response = self.client.post(ROUTE, content=content, headers=headers)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("do-not-echo", response.text)

    def test_body_byte_limit_with_and_without_content_length(self) -> None:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        for content in (b"x" * (api.MAX_CALLBACK_BYTES + 1), iter([b"x" * 20000, b"x" * 20000])):
            response = self.client.post(ROUTE, content=content, headers=headers)
            self.assertEqual(response.status_code, 400)
        response = self.client.post(
            ROUTE, content=b"x" * (api.MAX_CALLBACK_BYTES + 1), headers={**headers, "Content-Length": "1"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored()["decision"], "pending")

    def test_content_type_encoding_and_invalid_content_length_are_400(self) -> None:
        base = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        for headers in (
            {**base, "Content-Type": "text/plain"}, {**base, "Content-Encoding": "gzip"},
            {**base, "Content-Length": "-1"}, {**base, "Content-Length": "invalid"},
        ):
            response = self.client.post(ROUTE, content=json.dumps(self.body), headers=headers)
            self.assertEqual(response.status_code, 400)

    def test_authentication_dependency_is_exposed_but_not_disabled_by_default(self) -> None:
        self.assertEqual(self.client.post(ROUTE, json=self.body).status_code, 401)
        self.app.dependency_overrides[api.authenticate_callback] = lambda: {"oid": PRINCIPAL, "tid": TENANT}
        response = self.client.post(ROUTE, json=self.body)
        self.assertEqual(response.status_code, 200)
        self.urlopen.assert_not_called()

    def test_openapi_documents_bounded_callback_schema_and_statuses(self) -> None:
        operation = self.app.openapi()["paths"][ROUTE]["post"]
        self.assertTrue(operation["requestBody"]["required"])
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["workflow_run_id"]["maxLength"], 512)
        for status in ("200", "400", "401", "403", "404", "409", "503"):
            self.assertIn(status, operation["responses"])


if __name__ == "__main__":
    unittest.main()