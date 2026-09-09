"""Offline tests only: fake credentials/HTTP and mocked PowerShell Graph calls.

Run with python -m pytest tests/test_agent_identity_unit.py -q -p no:cacheprovider.
PowerShell checks skip where neither pwsh nor powershell is installed. They do
not load Az, log in, deploy, contact Graph, or change any tenant permissions.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import traceback
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from src import agent_identity as identity


TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
AGENT = "33333333-3333-3333-3333-333333333333"
MI = "44444444-4444-4444-4444-444444444444"
OBJECT = "55555555-5555-5555-5555-555555555555"
SCOPE = "https://storage.azure.com/.default"
ROOT = Path(__file__).resolve().parents[1]


def response(token="resource-token", expires=3600, status=200, body=None):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    if body is None:
        body = json.dumps({"access_token": token, "expires_in": expires, "token_type": "Bearer"}).encode()
    result.iter_content.return_value = [body]
    return result


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in (
        "AGENT_IDENTITY_ENABLED", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
        "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID",
        "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(identity.time, "time", lambda: 1000)
    # Any unmocked requests or default credential use must fail, never use local auth.
    monkeypatch.setattr(identity.requests.Session, "send", Mock(side_effect=AssertionError("Unexpected network")))
    monkeypatch.setattr(identity, "DefaultAzureCredential", Mock())
    monkeypatch.setattr(identity, "WorkloadIdentityCredential", Mock())


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "AGENT_IDENTITY_ENABLED": "true", "AZURE_CLIENT_ID": MI,
        "AZURE_TENANT_ID": TENANT, "AGENT_IDENTITY_APP_ID": AGENT,
        "AGENT_IDENTITY_BLUEPRINT_APP_ID": BLUEPRINT,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def credential(monkeypatch):
    bootstrap = Mock()
    bootstrap.get_token.return_value = AccessToken("bootstrap-token", 5000)
    session = Mock()
    session.post.side_effect = [response("blueprint-token"), response()]
    monkeypatch.setattr(identity.requests, "Session", Mock(return_value=session))
    with identity.AgentIdentityCredential(TENANT, BLUEPRINT, AGENT, bootstrap_credential=bootstrap) as value:
        yield value, bootstrap, session


@pytest.mark.parametrize("flag", [None, "false", "FALSE", "0"])
def test_disabled_factory(flag, monkeypatch):
    if flag is not None:
        monkeypatch.setenv("AGENT_IDENTITY_ENABLED", flag)
    assert identity.get_agent_credential() is identity.DefaultAzureCredential.return_value


@pytest.mark.parametrize("flag", ["", "tru", "yes", "enabled"])
def test_invalid_flag_fails_closed(flag, monkeypatch):
    monkeypatch.setenv("AGENT_IDENTITY_ENABLED", flag)
    with pytest.raises(ValueError):
        identity.get_agent_credential()
    identity.DefaultAzureCredential.assert_not_called()


@pytest.mark.parametrize("missing", ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID"])
def test_enabled_missing_configuration_does_not_fall_back(configured, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        identity.get_agent_credential()
    identity.DefaultAzureCredential.assert_not_called()


def test_factory_keeps_bootstrap_mi_id(configured):
    with identity.get_agent_credential() as value:
        assert isinstance(value, identity.AgentIdentityCredential)
        assert value.agent_app_id == AGENT
        assert identity.DefaultAzureCredential.call_args.kwargs["managed_identity_client_id"] == MI
        assert identity.DefaultAzureCredential.call_args.kwargs["retry_total"] == 0
    identity.DefaultAzureCredential.return_value.close.assert_called_once()
    assert os.environ["AZURE_CLIENT_ID"] == MI


def test_async_adapter_offloads_token_and_close():
    sync = Mock()
    sync.get_token.return_value = AccessToken("agent-token", 5000)

    async def exercise():
        async_credential = identity.AsyncAgentIdentityCredential(sync)
        assert await async_credential.get_token(SCOPE) == AccessToken("agent-token", 5000)
        await async_credential.close()

    import asyncio
    asyncio.run(exercise())
    sync.get_token.assert_called_once_with(SCOPE)
    sync.close.assert_called_once_with()


def test_async_factory_requires_enabled_agent_identity(configured, monkeypatch):
    monkeypatch.setattr(identity, "AsyncAgentIdentityCredential", Mock())
    assert identity.get_async_agent_credential() is identity.AsyncAgentIdentityCredential.return_value
    monkeypatch.setenv("AGENT_IDENTITY_ENABLED", "false")
    with pytest.raises(ValueError, match="false"):
        identity.get_async_agent_credential()


def test_workload_identity_is_selected_without_default_fallback(configured, monkeypatch):
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "projected-token")
    with identity.get_agent_credential():
        identity.WorkloadIdentityCredential.assert_called_once_with(
            tenant_id=TENANT, client_id=MI, token_file_path="projected-token",
            connection_timeout=5, read_timeout=10, retry_total=0,
        )
        identity.DefaultAzureCredential.assert_not_called()


@pytest.mark.parametrize("client_id", [AGENT, BLUEPRINT])
def test_bootstrap_cannot_be_agent_or_blueprint(configured, monkeypatch, client_id):
    monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    with pytest.raises(ValueError, match="bootstrap"):
        identity.get_agent_credential()


def test_nonpublic_authority_rejected(configured, monkeypatch):
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://attacker.example")
    with pytest.raises(ValueError, match="public cloud"):
        identity.get_agent_credential()


def test_invalid_ids_do_not_echo_values(configured, monkeypatch):
    monkeypatch.setenv("AGENT_IDENTITY_APP_ID", "secret-misconfigured-value")
    with pytest.raises(ValueError) as failure:
        identity.get_agent_credential()
    assert "secret-misconfigured-value" not in str(failure.value)
    identity.DefaultAzureCredential.assert_not_called()


def test_agent_and_blueprint_must_be_distinct(configured, monkeypatch):
    monkeypatch.setenv("AGENT_IDENTITY_APP_ID", BLUEPRINT)
    with pytest.raises(ValueError, match="distinct"):
        identity.get_agent_credential()


def test_workload_failure_never_uses_default_credential(configured, monkeypatch):
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "projected-token")
    identity.WorkloadIdentityCredential.return_value.get_token.side_effect = RuntimeError("secret")
    with identity.get_agent_credential() as value:
        with pytest.raises(ClientAuthenticationError, match="bootstrap"):
            value.get_token(SCOPE)
    identity.DefaultAzureCredential.assert_not_called()


def test_actual_two_stage_protocol_and_token_credential_result(credential):
    value, bootstrap, session = credential
    token = value.get_token(SCOPE)
    assert token == AccessToken("resource-token", 4600)
    bootstrap.get_token.assert_called_once_with("api://AzureADTokenExchange/.default")
    calls = session.post.call_args_list
    assert len(calls) == 2
    first, second = [call.kwargs["data"] for call in calls]
    assert first == {
        "client_id": BLUEPRINT, "fmi_path": AGENT,
        "scope": "api://AzureADTokenExchange/.default", "grant_type": "client_credentials",
        "client_assertion_type": identity._ASSERTION_TYPE, "client_assertion": "bootstrap-token",
    }
    assert second == {
        "client_id": AGENT, "scope": SCOPE, "grant_type": "client_credentials",
        "client_assertion_type": identity._ASSERTION_TYPE, "client_assertion": "blueprint-token",
    }
    for call in calls:
        assert call.args == (f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",)
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["timeout"] == (5, 10)
        assert call.kwargs["stream"] is True
        assert "client_secret" not in call.kwargs["data"]


def test_cache_per_resource_and_shared_blueprint_token(credential):
    value, bootstrap, session = credential
    first = value.get_token(SCOPE)
    assert value.get_token(SCOPE) is first
    session.post.side_effect = [response("search-token")]
    assert value.get_token("https://search.azure.com/.default").token == "search-token"
    assert session.post.call_count == 3
    bootstrap.get_token.assert_called_once()


def test_cache_refresh_before_expiry(credential, monkeypatch):
    value, bootstrap, session = credential
    value.get_token(SCOPE)
    monkeypatch.setattr(identity.time, "time", lambda: 4540)
    bootstrap.get_token.return_value = AccessToken("fresh-bootstrap", 8000)
    session.post.side_effect = [response("fresh-blueprint"), response("fresh-resource")]
    assert value.get_token(SCOPE) == AccessToken("fresh-resource", 8140)
    assert session.post.call_count == 4


def test_only_expiring_resource_is_refreshed(credential):
    value, bootstrap, session = credential
    session.post.side_effect = [response("blueprint", 7200), response("short", 30), response("new")]
    assert value.get_token(SCOPE).token == "short"
    assert value.get_token(SCOPE).token == "new"
    assert session.post.call_count == 3
    bootstrap.get_token.assert_called_once()


def test_concurrent_calls_share_cache(credential):
    value, bootstrap, session = credential
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: value.get_token(SCOPE), range(16)))
    assert all(token == tokens[0] for token in tokens)
    assert session.post.call_count == 2
    bootstrap.get_token.assert_called_once()


def test_cache_has_bounded_size(credential):
    value, _, session = credential
    session.post.side_effect = lambda *args, **kwargs: response()
    for number in range(40):
        value.get_token(f"https://resource{number}.example/.default")
    assert len(value._tokens) == 32
    assert "https://resource0.example/.default" not in value._tokens


@pytest.mark.parametrize("scopes", [(), (SCOPE, SCOPE), ("https://storage.azure.com",), ("/.default",), ("x y/.default",), (identity._EXCHANGE_SCOPE,)])
def test_scope_validation(credential, scopes):
    value, bootstrap, session = credential
    with pytest.raises(ValueError):
        value.get_token(*scopes)
    bootstrap.get_token.assert_not_called()
    session.post.assert_not_called()


@pytest.mark.parametrize("options", [{"claims": "secret-claims"}, {"claims": ""}, {"enable_cae": True}, {"tenant_id": MI}])
def test_unsupported_options_rejected_even_with_cached_token(credential, options):
    value, _, session = credential
    value.get_token(SCOPE)
    with pytest.raises(ClientAuthenticationError) as failure:
        value.get_token(SCOPE, **options)
    assert "secret-claims" not in str(failure.value)
    assert session.post.call_count == 2


def test_same_tenant_allowed(credential):
    assert credential[0].get_token(SCOPE, tenant_id=TENANT).token == "resource-token"


def test_unknown_options_fail_before_network(credential):
    with pytest.raises(TypeError):
        credential[0].get_token(SCOPE, unsupported="secret")
    credential[2].post.assert_not_called()


@pytest.mark.parametrize("token", [AccessToken("", 5000), AccessToken("expired", 1000)])
def test_invalid_bootstrap_tokens_fail_closed(credential, token):
    credential[1].get_token.return_value = token
    with pytest.raises(ClientAuthenticationError, match="bootstrap"):
        credential[0].get_token(SCOPE)
    credential[2].post.assert_not_called()


def test_bootstrap_error_is_secret_safe(credential):
    value, bootstrap, session = credential
    bootstrap.get_token.side_effect = RuntimeError("secret-bootstrap-assertion")
    with pytest.raises(ClientAuthenticationError) as failure:
        value.get_token(SCOPE)
    assert "secret-bootstrap-assertion" not in "".join(traceback.format_exception(failure.value))
    session.post.assert_not_called()


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_http_errors_are_secret_safe_without_fallback_or_retry(credential, stage, status):
    value, bootstrap, session = credential
    session.post.side_effect = [response("blueprint")] * stage + [response(status=status, body=b"secret-token")]
    with pytest.raises(ClientAuthenticationError, match=f"HTTP {status}") as failure:
        value.get_token(SCOPE)
    assert "secret-token" not in str(failure.value)
    assert session.post.call_count == stage + 1
    assert not value._tokens
    bootstrap.get_token.assert_called_once_with(identity._EXCHANGE_SCOPE)


@pytest.mark.parametrize("body", [b"not json", b"{}", b"[]", b'{"error":"secret"}', b"x" * (128 * 1024 + 1)])
def test_invalid_or_oversized_response(credential, body):
    value, _, session = credential
    session.post.side_effect = [response(body=body)]
    with pytest.raises(ClientAuthenticationError, match="invalid response"):
        value.get_token(SCOPE)


@pytest.mark.parametrize("expires", [0, -10, True, "bad", 1.5, None])
def test_invalid_expiry(credential, expires):
    value, _, session = credential
    session.post.side_effect = [response(expires=expires)]
    with pytest.raises(ClientAuthenticationError):
        value.get_token(SCOPE)


def test_transport_error_does_not_expose_assertion(credential):
    value, _, session = credential
    session.post.side_effect = identity.requests.Timeout("secret-in-request")
    with pytest.raises(ClientAuthenticationError) as failure:
        value.get_token(SCOPE)
    assert "secret-in-request" not in "".join(traceback.format_exception(failure.value))


def test_slow_stream_fails_deadline_and_closes_response(credential, monkeypatch):
    value, _, session = credential
    http_response = response()
    session.post.side_effect = [http_response]
    clock = iter([0, 31])
    monkeypatch.setattr(identity.time, "monotonic", lambda: next(clock))
    with pytest.raises(ClientAuthenticationError):
        value.get_token(SCOPE)
    http_response.__exit__.assert_called_once()


def test_string_expiry_is_supported(credential):
    value, _, session = credential
    session.post.side_effect = [response("blueprint", "3600"), response(expires="3600")]
    assert value.get_token(SCOPE).expires_on == 4600


def test_failed_resource_exchange_invalidates_blueprint(credential):
    value, bootstrap, session = credential
    session.post.side_effect = [response("old-blueprint"), response(status=401)]
    with pytest.raises(ClientAuthenticationError):
        value.get_token(SCOPE)
    session.post.side_effect = [response("new-blueprint"), response()]
    value.get_token(SCOPE)
    assert bootstrap.get_token.call_count == 2


def test_close_clears_cache_and_does_not_close_borrowed_bootstrap(credential):
    value, bootstrap, session = credential
    value.get_token(SCOPE)
    value.close()
    value.close()
    assert not value._tokens and value._blueprint_token is None
    session.close.assert_called_once()
    bootstrap.close.assert_not_called()
    with pytest.raises(ClientAuthenticationError, match="closed"):
        value.get_token(SCOPE)


# Execute the actual embedded provisioning PowerShell, replacing its two external
# boundaries with strict mocks. No Az installation or real auth can be invoked.
def run_provisioning(module, config, steps):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell not available for offline provisioning tests")
    source = (ROOT / "infra/core/identity" / module).read_text(encoding="utf-8")
    scripts = re.findall(r"scriptContent: '''\n(.*?)\n\s*'''", source, re.S)
    assert len(scripts) == 1, "Each module must contain exactly one provisioning script"
    script = scripts[0]
    prelude = r"""
    $ErrorActionPreference = 'Stop'
    $script:steps = @($env:MOCK_STEPS | ConvertFrom-Json)
    $script:index = 0
    $script:calls = @()
    function Get-AzAccessToken {
      param($ResourceUrl, $TenantId, $ErrorAction)
      $script:calls += @{ kind = 'bootstrap'; resource = $ResourceUrl; tenant = $TenantId }
      @{ Token = (ConvertTo-SecureString 'mock-mi-token' -AsPlainText -Force) }
    }
    function Invoke-RestMethod {
      param($Method, $Uri, $Headers, $Body, $ContentType, $TimeoutSec, $MaximumRedirection, $ErrorAction)
      if ($script:index -ge $script:steps.Count) { throw 'Unexpected network call blocked' }
      $step = $script:steps[$script:index++]
      $parsedBody = $Body
      if ($Body -is [string]) { $parsedBody = $Body | ConvertFrom-Json }
      $script:calls += @{ kind = 'http'; method = $Method; uri = $Uri; headers = $Headers; body = $parsedBody; timeout = $TimeoutSec; redirects = $MaximumRedirection }
      if ($step.method -ne $Method -or $Uri -notmatch $step.uri) { throw 'Mock request mismatch' }
      if ($step.fail) { throw 'mock-secret-error-body' }
      $step.response
    }
    """
    wrapped = textwrap.dedent(prelude) + "\ntry {\n" + textwrap.dedent(script) + r"""
      @{ ok = $true; outputs = $DeploymentScriptOutputs; calls = $script:calls } | ConvertTo-Json -Depth 30 -Compress
    } catch {
      @{ ok = $false; error = $_.Exception.Message; calls = $script:calls } | ConvertTo-Json -Depth 30 -Compress
    }
    """
    env = dict(os.environ, IDENTITY_CONFIG=json.dumps(config), MOCK_STEPS=json.dumps(steps))
    # Pass one complete script, not stdin's line-by-line interactive parser.
    # No temporary scripts or workspace writes.
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", wrapped],
        text=True, capture_output=True, env=env, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    # pwsh may emit terminal-control sequences even when redirected.
    stdout = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", completed.stdout).strip()
    assert stdout, completed.stderr
    return json.loads(stdout)


def step(method, uri, result=None, fail=False):
    return {"method": method, "uri": uri, "response": result, "fail": fail}


def blueprint_config(**updates):
    return dict({
        "cloud": "AzureCloud", "tenantId": TENANT, "principalId": MI,
        "displayName": "Agent's blueprint", "uniqueName": "stable-blueprint",
        "existingAppId": "", "sponsorUsers": [MI], "sponsorGroups": [OBJECT],
        "ownerUsers": [MI], "ownerServicePrincipals": [], "scope": "access_agent",
    }, **updates)


def app_result():
    return {"id": OBJECT, "appId": BLUEPRINT, "identifierUris": [f"api://{BLUEPRINT}"],
            "tags": ["azure-agents-control-plane:stable-blueprint"],
            "api": {"oauth2PermissionScopes": [{"id": AGENT, "value": "access_agent", "isEnabled": True}]}}


@pytest.mark.parametrize("drift", [False, True])
def test_blueprint_reuse_repairs_missing_principal_and_fic(drift):
    fic = {"id": "fic", "name": "mcp-agent-msi", "issuer": "wrong", "subject": MI, "audiences": ["wrong"]}
    steps = [
        step("GET", r"applications/microsoft.graph.agentIdentityBlueprint$", {"value": [app_result()]}),
        step("GET", r"servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal\?", {"value": []}),
        step("POST", r"servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal$", {"id": MI}),
        step("GET", r"federatedIdentityCredentials$", {"value": [fic] if drift else []}),
        step("PATCH" if drift else "POST", r"federatedIdentityCredentials(/fic)?$", {"id": "fic"}),
        step("GET", r"/microsoft.graph.agentIdentityBlueprint$", app_result()),
    ]
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(), steps)
    assert result["ok"], result
    assert result["outputs"]["blueprintObjectId"] == OBJECT
    write = [call for call in result["calls"] if call.get("method") in ("POST", "PATCH")][-1]
    assert write["body"]["issuer"] == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert write["body"]["subject"] == MI
    assert write["body"]["audiences"] == ["api://AzureADTokenExchange"]


def test_blueprint_creation_and_scope_preservation():
    app = app_result()
    app["api"]["oauth2PermissionScopes"][0]["value"] = "unrelated"
    steps = [
        step("GET", "agentIdentityBlueprint$", {"value": []}),
        step("GET", "displayName", {"value": []}),
        step("POST", r"applications/microsoft.graph.agentIdentityBlueprint$", app),
        step("GET", "agentIdentityBlueprintPrincipal", {"value": [{"id": MI}]}),
        step("GET", "federatedIdentityCredentials", {"value": []}),
        step("POST", "federatedIdentityCredentials", {"id": "fic"}),
        step("GET", r"/microsoft.graph.agentIdentityBlueprint$", app),
        step("PATCH", r"/microsoft.graph.agentIdentityBlueprint$"),
    ]
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(), steps)
    assert result["ok"], result
    writes = [call for call in result["calls"] if call.get("method") in ("POST", "PATCH")]
    assert writes[0]["body"]["tags"] == ["azure-agents-control-plane:stable-blueprint"]
    assert writes[0]["body"]["sponsors@odata.bind"] == [
        f"https://graph.microsoft.com/v1.0/users/{MI}", f"https://graph.microsoft.com/v1.0/groups/{OBJECT}",
    ]
    scopes = writes[-1]["body"]["api"]["oauth2PermissionScopes"]
    assert scopes[0]["id"] == AGENT and scopes[0]["value"] == "unrelated"
    assert scopes[1]["value"] == "access_agent"


def test_blueprint_forbidden_lookup_is_not_treated_as_not_found():
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(), [step("GET", "applications", fail=True)])
    assert not result["ok"]
    assert "mock-secret" not in result["error"]
    assert len(result["calls"]) == 2


def test_blueprint_missing_sponsors_never_creates():
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(sponsorUsers=[], sponsorGroups=[]), [
        step("GET", "agentIdentityBlueprint$", {"value": []}),
        step("GET", "displayName", {"value": []}),
    ])
    assert not result["ok"] and "sponsor" in result["error"]
    assert all(call.get("method", "GET") == "GET" for call in result["calls"])


def test_blueprint_duplicate_deployment_tag_never_creates():
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(), [
        step("GET", "agentIdentityBlueprint$", {"value": [app_result(), app_result()]}),
    ])
    assert not result["ok"] and "Ambiguous" in result["error"]
    assert len(result["calls"]) == 2


def test_blueprint_explicit_adoption_has_no_writes_if_correct():
    fic = {"id": "fic", "name": "mcp-agent-msi", "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0", "subject": MI, "audiences": ["api://AzureADTokenExchange"]}
    result = run_provisioning("agentIdentityBlueprint.bicep", blueprint_config(existingAppId=BLUEPRINT), [
        step("GET", "applications\\(appId=", app_result()),
        step("GET", "agentIdentityBlueprintPrincipal", {"value": [{"id": MI}]}),
        step("GET", "federatedIdentityCredentials", {"value": [fic]}),
        step("GET", r"/microsoft.graph.agentIdentityBlueprint$", app_result()),
    ])
    assert result["ok"], result
    assert all(call.get("method", "GET") == "GET" for call in result["calls"])


@pytest.mark.parametrize("existing", [False, True])
def test_agent_creation_uses_blueprint_token_and_agent_id_without_appid(existing):
    config = {"cloud": "AzureCloud", "tenantId": TENANT, "blueprintAppId": BLUEPRINT,
              "displayName": "Agent's name", "existingId": AGENT if existing else "",
              "sponsorUsers": [MI], "sponsorGroups": [OBJECT]}
    agent = {"id": AGENT, "agentIdentityBlueprintId": BLUEPRINT, "displayName": "Agent's name"}
    steps = [step("POST", r"/oauth2/v2.0/token$", {"access_token": "mock-blueprint-token", "token_type": "Bearer"})]
    if existing:
        steps += [step("GET", f"servicePrincipals/{AGENT}/microsoft.graph.agentIdentity$", agent)]
    else:
        steps += [step("GET", "agentIdentity\\?", {"value": []}), step("POST", "agentIdentity$", agent)]
    result = run_provisioning("agentIdentity.bicep", config, steps)
    assert result["ok"], result
    assert result["outputs"]["agentIdentityAppId"] == AGENT
    assert result["calls"][0]["resource"] == "api://AzureADTokenExchange"
    token_form = result["calls"][1]["body"]
    assert token_form["client_id"] == BLUEPRINT
    assert token_form["client_assertion"] == "mock-mi-token"
    assert token_form["scope"] == "https://graph.microsoft.com/.default"
    assert "fmi_path" not in token_form
    for call in result["calls"][2:]:
        assert call["headers"]["Authorization"] == "Bearer mock-blueprint-token"
        assert call["timeout"] == 30 and call["redirects"] == 0


def test_agent_existing_wrong_parent_fails():
    config = {"cloud": "AzureCloud", "tenantId": TENANT, "blueprintAppId": BLUEPRINT, "existingId": AGENT}
    result = run_provisioning("agentIdentity.bicep", config, [
        step("POST", "token$", {"access_token": "mock-blueprint-token", "token_type": "Bearer"}),
        step("GET", "agentIdentity$", {"id": AGENT, "agentIdentityBlueprintId": MI}),
    ])
    assert not result["ok"] and "different blueprint" in result["error"]


@pytest.mark.parametrize("mode", ["create", "exists", "drift", "wrong-blueprint"])
def test_aks_fic_targets_blueprint_and_reconciles(mode):
    config = {"cloud": "AzureCloud", "tenantId": TENANT, "objectId": OBJECT, "appId": BLUEPRINT,
              "issuer": "https://aks.example/issuer/", "subject": "system:serviceaccount:ns:agent", "name": "aks-agent"}
    fic = {"id": "fic", "name": config["name"], "issuer": config["issuer"], "subject": config["subject"], "audiences": ["api://AzureADTokenExchange"]}
    if mode == "drift":
        fic["issuer"] = "https://old-issuer.example/"
    steps = [step("GET", f"applications/{OBJECT}/microsoft.graph.agentIdentityBlueprint$", {"appId": MI if mode == "wrong-blueprint" else BLUEPRINT})]
    if mode != "wrong-blueprint":
        steps += [step("GET", "federatedIdentityCredentials$", {"value": [] if mode == "create" else [fic]})]
        if mode in ("create", "drift"):
            steps += [step("POST" if mode == "create" else "PATCH", "federatedIdentityCredentials", {"id": "fic"})]
    result = run_provisioning("aksFederatedCredential.bicep", config, steps)
    if mode == "wrong-blueprint":
        assert not result["ok"] and "mismatch" in result["error"]
    else:
        assert result["ok"], result
        assert result["outputs"]["status"] == {"create": "created", "exists": "exists", "drift": "updated"}[mode]
        assert all("servicePrincipals" not in call.get("uri", "") for call in result["calls"])