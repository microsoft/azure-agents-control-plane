"""Authenticated Logic App callback router; mount with app.include_router(router).

Dependencies: FastAPI, Pydantic 2, and PyJWT[crypto]>=2.10.1,<3. Authentication is
performed in the application even behind APIM. Only the configured tenant's
public-cloud JWKS endpoint is used; no proxy identity headers are trusted.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWKClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

if __package__:
    from .agent365_approval import (
        ApprovalError, ApprovalWorkflowEngine, CallbackAuthSettings,
        get_approval_workflow_engine, get_callback_auth_settings,
    )
else:
    from agent365_approval import (
        ApprovalError, ApprovalWorkflowEngine, CallbackAuthSettings,
        get_approval_workflow_engine, get_callback_auth_settings,
    )


router = APIRouter()
MAX_CALLBACK_BYTES = 32 * 1024
MAX_BEARER_LENGTH = 16 * 1024
_GUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


class ApprovalCallbackPayload(BaseModel):
    """Bounded input; no coercion, routing overrides, or displayName fields."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    approval_id: str = Field(min_length=36, max_length=36, pattern=f"^{_GUID_PATTERN}$")
    environment: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    request_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision: str = Field(min_length=1, max_length=16)
    approved_by: str | None = Field(default=None, max_length=36)
    approver_tenant_id: str | None = Field(default=None, max_length=36)
    comment: str | None = Field(default=None, max_length=4096)
    workflow_run_id: str = Field(min_length=1, max_length=512)
    timestamp: str | None = Field(default=None, min_length=1, max_length=64)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="Invalid or missing callback bearer token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


@lru_cache(maxsize=4)
def _get_jwks_client(tenant_id: str) -> PyJWKClient:
    # tenant_id comes only from validated server configuration. In particular,
    # iss, jku, x5u, and tid in an unverified JWT never select a network URL.
    return PyJWKClient(
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
        cache_jwk_set=True, lifespan=300, cache_keys=False, timeout=5,
    )


def _verify_callback_token(token: str, settings: CallbackAuthSettings) -> dict[str, Any]:
    """Verify real RS256 signatures; this synchronous work runs off the event loop."""
    try:
        header = jwt.get_unverified_header(token)
    except (InvalidTokenError, ValueError, TypeError, RecursionError):
        raise _unauthorized() from None
    if (
        header.get("alg") != "RS256" or not isinstance(header.get("kid"), str)
        or not 1 <= len(header["kid"]) <= 128 or "jku" in header or "x5u" in header
        or header.get("crit")
    ):
        raise _unauthorized()
    try:
        key = _get_jwks_client(settings.tenant_id).get_signing_key_from_jwt(token)
    except PyJWKClientConnectionError:
        raise HTTPException(status_code=503, detail="Callback signing keys are unavailable.") from None
    except (PyJWKClientError, InvalidTokenError):
        raise _unauthorized() from None
    except Exception:
        raise HTTPException(status_code=503, detail="Callback signing keys are unavailable.") from None
    issuers = (
        f"https://sts.windows.net/{settings.tenant_id}/",
        f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0",
    )
    try:
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=list(settings.audiences),
            issuer=list(issuers), leeway=0,
            options={
                "require": ["exp", "iss", "aud", "tid", "oid", "ver"],
                "verify_signature": True, "verify_exp": True, "verify_nbf": True,
                "verify_iat": True, "verify_aud": True, "verify_iss": True,
            },
        )
    except (InvalidTokenError, ValueError, TypeError, RecursionError):
        raise _unauthorized() from None
    except Exception:
        raise HTTPException(status_code=503, detail="Callback token verification is unavailable.") from None
    # Require actual scalar identity/audience claims, not coercible values.
    # v1 managed-identity tokens use the resource URI; v2 can use that URI or
    # the blueprint UUID. Bind each token version to its exact tenant issuer.
    version = claims.get("ver")
    tenant = claims.get("tid")
    principal = claims.get("oid")
    if (
        type(claims.get("exp")) is not int
        or not isinstance(tenant, str) or not re.fullmatch(_GUID_PATTERN, tenant)
        or tenant.lower() != settings.tenant_id
        or not isinstance(principal, str) or not re.fullmatch(_GUID_PATTERN, principal)
        or not isinstance(claims.get("aud"), str)
        or version not in ("1.0", "2.0")
        or claims["iss"] != issuers[0 if version == "1.0" else 1]
        or (version == "1.0" and claims["aud"] != settings.audience)
    ):
        raise _unauthorized()
    if (
        principal.lower() != settings.principal_id or "scp" in claims
        or claims.get("idtyp", "app") != "app"
    ):
        raise HTTPException(status_code=403, detail="Callback principal is not authorized.")
    return claims


async def authenticate_callback(request: Request) -> dict[str, Any]:
    """Dependency exposed for tests; authorize ONLY the Logic App system MI oid."""
    values = request.headers.getlist("authorization")
    if len(values) != 1 or len(values[0]) > MAX_BEARER_LENGTH + 16:
        raise _unauthorized()
    parts = values[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > MAX_BEARER_LENGTH:
        raise _unauthorized()
    try:
        settings = get_callback_auth_settings()
    except ApprovalError:
        raise HTTPException(status_code=503, detail="Callback authentication is not configured.") from None
    return await asyncio.to_thread(_verify_callback_token, parts[1], settings)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON number")


async def _read_payload(request: Request) -> ApprovalCallbackPayload:
    """Limit streaming bytes BEFORE JSON/Pydantic parsing, even without length."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=400, detail="Callback body must be JSON.")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(status_code=400, detail="Encoded callback bodies are not supported.")
    lengths = request.headers.getlist("content-length")
    if lengths and (
        len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
        or len(lengths[0]) > 10 or int(lengths[0]) > MAX_CALLBACK_BYTES
    ):
        raise HTTPException(status_code=400, detail="Invalid callback body size.")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_CALLBACK_BYTES:
                raise HTTPException(status_code=400, detail="Invalid callback body size.")
            body.extend(chunk)
        data = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
        )
        return ApprovalCallbackPayload.model_validate(data)
    except (ValidationError, ValueError, UnicodeError, RecursionError, ClientDisconnect):
        # Validation errors can echo user-provided secrets; never serialize the
        # Pydantic error array or raw JSON/headers in this endpoint's response.
        raise HTTPException(status_code=400, detail="Invalid approval callback payload.") from None


@router.post(
    "/approvals/callback",
    responses={
        400: {"description": "Invalid decision or payload"},
        401: {"description": "Invalid bearer token"},
        403: {"description": "Unauthorized principal or approver"},
        404: {"description": "Unknown approval/environment"},
        409: {"description": "Conflicting or expired approval"},
        503: {"description": "Configuration, JWKS, or persistence unavailable"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ApprovalCallbackPayload.model_json_schema()}},
        },
    },
)
async def approval_callback(
    request: Request,
    authenticated: dict[str, Any] = Depends(authenticate_callback),
    engine: ApprovalWorkflowEngine = Depends(get_approval_workflow_engine),
) -> dict[str, Any]:
    """Acknowledge only a durable terminal result, never merely receipt/enqueue."""
    payload = await _read_payload(request)
    try:
        contract = await engine.process_approval_response(**payload.model_dump())
        if not contract.is_complete():
            raise HTTPException(status_code=503, detail="Approval response is not durably complete.")
        return contract.to_dict()
    except HTTPException:
        raise
    except ApprovalError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Approval callback could not be persisted.") from None


__all__ = ["router", "authenticate_callback", "ApprovalCallbackPayload"]