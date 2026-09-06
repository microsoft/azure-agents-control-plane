"""Synchronous Azure TokenCredential for autonomous Microsoft Entra Agent ID.

Protocol (checked 2026-09-05):
https://learn.microsoft.com/entra/agent-id/agent-autonomous-app-oauth-flow
https://learn.microsoft.com/entra/agent-id/autonomous-agent-authentication-authorization-flow

Bootstrap MI -> blueprint exchange token (fmi_path=agent) -> resource token.
Both exchanges use client_credentials/client_assertion, NOT grant_type=agent_fic.
No user/OBO, claims challenges, CAE, cross-tenant or sovereign-cloud support.
No Graph permissions are needed merely to acquire downstream Azure tokens;
the agent principal must separately have the resource's Azure RBAC permissions.

Create once with get_agent_credential(), share across synchronous Azure clients,
and close at shutdown. Async callers must offload get_token (or use an async
adapter); this is not an azure.core.credentials_async.AsyncTokenCredential.
Dependencies azure-identity, azure-core and requests already exist in the app.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any
from uuid import UUID

import requests
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential, WorkloadIdentityCredential

_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_AUTHORITY = "https://login.microsoftonline.com"
_REFRESH_SKEW = 60
_CACHE_SIZE = 32
_MAX_RESPONSE_BYTES = 128 * 1024


def _guid(value: str | None, name: str) -> str:
    try:
        return str(UUID(value or ""))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{name} must be a GUID; agent identity is not disabled automatically") from None


class AgentIdentityCredential:
    """Fail-closed, single-tenant TokenCredential with in-memory token caching.

    Explicit bootstrap credentials are borrowed, not closed by this object.
    Otherwise AZURE_CLIENT_ID always identifies the bootstrap managed identity,
    never the blueprint or agent. With AZURE_FEDERATED_TOKEN_FILE configured,
    WorkloadIdentityCredential is selected directly (no developer fallback).
    Else DefaultAzureCredential is used only to acquire the bootstrap assertion.
    A local user token normally cannot satisfy a managed-identity blueprint FIC.
    """

    def __init__(
        self,
        tenant_id: str | None = None,
        blueprint_app_id: str | None = None,
        agent_app_id: str | None = None,
        *,
        bootstrap_credential: TokenCredential | None = None,
    ) -> None:
        self.tenant_id = _guid(tenant_id or os.getenv("AZURE_TENANT_ID"), "AZURE_TENANT_ID")
        self.blueprint_app_id = _guid(
            blueprint_app_id or os.getenv("AGENT_IDENTITY_BLUEPRINT_APP_ID"),
            "AGENT_IDENTITY_BLUEPRINT_APP_ID",
        )
        self.agent_app_id = _guid(
            agent_app_id or os.getenv("AGENT_IDENTITY_APP_ID"), "AGENT_IDENTITY_APP_ID"
        )
        if self.blueprint_app_id == self.agent_app_id:
            raise ValueError("Blueprint and agent identity must be distinct")
        if os.getenv("AZURE_AUTHORITY_HOST", _AUTHORITY).rstrip("/") != _AUTHORITY:
            raise ValueError("AgentIdentityCredential currently supports Azure public cloud only")

        self._owns_bootstrap = bootstrap_credential is None
        if bootstrap_credential is None:
            client_id = _guid(os.getenv("AZURE_CLIENT_ID"), "AZURE_CLIENT_ID")
            if client_id in (self.blueprint_app_id, self.agent_app_id):
                raise ValueError("AZURE_CLIENT_ID must remain the bootstrap managed identity client ID")
            options = {"connection_timeout": 5, "read_timeout": 10, "retry_total": 0}
            token_file = os.getenv("AZURE_FEDERATED_TOKEN_FILE")
            if token_file:
                bootstrap_credential = WorkloadIdentityCredential(
                    tenant_id=self.tenant_id, client_id=client_id,
                    token_file_path=token_file, **options,
                )
            else:
                bootstrap_credential = DefaultAzureCredential(
                    managed_identity_client_id=client_id,
                    exclude_interactive_browser_credential=True,
                    process_timeout=10, **options,
                )
        self._bootstrap = bootstrap_credential
        self._session = requests.Session()
        self._token_url = f"{_AUTHORITY}/{self.tenant_id}/oauth2/v2.0/token"
        self._blueprint_token: AccessToken | None = None
        self._tokens: OrderedDict[str, AccessToken] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _fresh(token: AccessToken | None) -> bool:
        return token is not None and token.expires_on > time.time() + _REFRESH_SKEW

    def _exchange(self, stage: str, fields: dict[str, str]) -> AccessToken:
        # One request per stage; no redirects or automatic retries can leak or
        # repeatedly replay an assertion. Never include bodies/headers/exceptions
        # in errors: even an Entra error_description may echo a credential.
        started = time.time()
        deadline = time.monotonic() + 30
        try:
            with self._session.post(
                self._token_url, data=fields, timeout=(5, 10),
                allow_redirects=False, stream=True,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status_code != 200:
                    raise ClientAuthenticationError(
                        message=f"Agent identity {stage} exchange failed (HTTP {int(response.status_code)}); "
                        "check Entra sign-in logs, federation, consent and tenant policy"
                    )
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise ValueError("Response limit exceeded")
                result = json.loads(body)
                token = result["access_token"]
                lifetime = result["expires_in"]
                if (
                    not isinstance(token, str) or not token.strip()
                    or str(result.get("token_type", "")).lower() != "bearer"
                    or isinstance(lifetime, bool)
                    or not isinstance(lifetime, (int, str))
                ):
                    raise ValueError("Invalid token response")
                expires_on = int(started) + int(lifetime)
                if expires_on <= time.time():
                    raise ValueError("Expired token response")
                return AccessToken(token, expires_on)
        except ClientAuthenticationError:
            raise
        except Exception:
            raise ClientAuthenticationError(
                message=f"Agent identity {stage} exchange failed or returned an invalid response"
            ) from None

    def get_token(
        self, *scopes: str, claims: str | None = None,
        tenant_id: str | None = None, enable_cae: bool = False, **kwargs: Any,
    ) -> AccessToken:
        """Return an agent resource token, never a bootstrap or blueprint token.

        Exactly one resource /.default scope is supported. Unsupported claims,
        CAE or tenant overrides fail before consulting the cache or network.
        """
        if claims is not None:
            raise ClientAuthenticationError(message="Agent identity claims challenges are not supported")
        if enable_cae:
            raise ClientAuthenticationError(message="Agent identity CAE is not supported")
        if tenant_id is not None and tenant_id.lower() != self.tenant_id:
            raise ClientAuthenticationError(message="Agent identity cross-tenant token requests are not supported")
        if kwargs:
            raise TypeError("Unsupported AgentIdentityCredential token options")
        if (
            len(scopes) != 1 or not isinstance(scopes[0], str)
            or not scopes[0].endswith("/.default")
            or len(scopes[0]) <= len("/.default")
            or any(c.isspace() for c in scopes[0])
            or scopes[0] == _EXCHANGE_SCOPE
        ):
            raise ValueError("Exactly one downstream resource /.default scope is required")
        scope = scopes[0]
        with self._lock:
            if self._closed:
                raise ClientAuthenticationError(message="AgentIdentityCredential is closed")
            cached = self._tokens.get(scope)
            if self._fresh(cached):
                self._tokens.move_to_end(scope)
                return cached
            if not self._fresh(self._blueprint_token):
                self._blueprint_token = None
                try:
                    bootstrap = self._bootstrap.get_token(_EXCHANGE_SCOPE)
                    if not bootstrap.token or bootstrap.expires_on <= time.time():
                        raise ValueError("Invalid bootstrap token")
                except Exception:
                    raise ClientAuthenticationError(
                        message="Agent identity bootstrap authentication failed; no resource credential fallback is allowed"
                    ) from None
                self._blueprint_token = self._exchange("blueprint", {
                    "client_id": self.blueprint_app_id,
                    "scope": _EXCHANGE_SCOPE,
                    "grant_type": "client_credentials",
                    "client_assertion_type": _ASSERTION_TYPE,
                    "client_assertion": bootstrap.token,
                    "fmi_path": self.agent_app_id,
                })
            try:
                token = self._exchange("resource", {
                    "client_id": self.agent_app_id,
                    "scope": scope,
                    "grant_type": "client_credentials",
                    "client_assertion_type": _ASSERTION_TYPE,
                    "client_assertion": self._blueprint_token.token,
                })
            except ClientAuthenticationError:
                self._blueprint_token = None
                self._tokens.pop(scope, None)
                raise
            self._tokens[scope] = token
            self._tokens.move_to_end(scope)
            while len(self._tokens) > _CACHE_SIZE:
                self._tokens.popitem(last=False)
            return token

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._tokens.clear()
                self._blueprint_token = None
                self._session.close()
                if self._owns_bootstrap:
                    self._bootstrap.close()

    def __enter__(self) -> AgentIdentityCredential:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def get_agent_credential() -> TokenCredential:
    """Select explicitly: disabled -> DefaultAzureCredential; enabled -> agent.

    Missing flag defaults to disabled for backwards compatibility. Misspellings
    fail closed instead of silently enabling legacy managed-identity access.
    """
    enabled = os.getenv("AGENT_IDENTITY_ENABLED", "false").strip().lower()
    if enabled in ("false", "0"):
        return DefaultAzureCredential()
    if enabled in ("true", "1"):
        return AgentIdentityCredential()
    raise ValueError("AGENT_IDENTITY_ENABLED must be true/false or 1/0")