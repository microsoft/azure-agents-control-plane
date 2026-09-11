"""Offline telemetry tests: no model, Graph, identity issuance or export calls."""

import ast
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import baggage, context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from src import agent_observability as obs

ROOT = Path(__file__).resolve().parents[1]
TENANT = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"
BLUEPRINT = "33333333-3333-3333-3333-333333333333"
UAMI = "44444444-4444-4444-4444-444444444444"
CONFIG = {"AGENT_OBSERVABILITY_MODE": "agent365", "AZURE_TENANT_ID": TENANT,
          "AGENT_IDENTITY_APP_ID": AGENT, "AGENT_IDENTITY_BLUEPRINT_APP_ID": BLUEPRINT,
          "AZURE_CLIENT_ID": UAMI, "AGENT_IDENTITY_ENABLED": "false"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, "send", Mock(side_effect=AssertionError("No network in telemetry tests")))


@pytest.fixture
def pipeline():
    # Isolated provider: do not replace the process-global OTel singleton.
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "offline"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = obs.AgentTelemetry()
    telemetry.config = obs.TelemetryConfig.from_environment(CONFIG)
    telemetry._tracer = provider.get_tracer("offline")
    telemetry._provider = provider
    yield telemetry, exporter
    telemetry.shutdown()


def test_disabled_has_no_sdk_import_credential_or_network(monkeypatch):
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert not name.startswith(("microsoft.opentelemetry", "opentelemetry", "azure"))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    telemetry = obs.AgentTelemetry()
    factory = Mock(side_effect=AssertionError("No credential when off"))
    telemetry.configure(environ={}, credential_factory=factory)
    with telemetry.invocation("mcp"), telemetry.tool("hello_mcp") as observation:
        observation.failed()
    telemetry.shutdown()
    factory.assert_not_called()


@pytest.mark.parametrize("mode", ["", "true", "yes", "AGENT365-invalid"])
def test_bad_mode_is_not_silently_enabled_or_disabled(mode):
    with pytest.raises(ValueError, match="AGENT_OBSERVABILITY_MODE"):
        obs.TelemetryConfig.from_environment({"AGENT_OBSERVABILITY_MODE": mode})


@pytest.mark.parametrize("name", ["AZURE_TENANT_ID", "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID"])
@pytest.mark.parametrize("value", ["", "not-an-id", "00000000-0000-0000-0000-000000000000"])
def test_real_export_requires_real_identifiers(name, value):
    with pytest.raises(ValueError, match=name):
        obs.TelemetryConfig.from_environment(dict(CONFIG, **{name: value}))


def test_telemetry_is_independent_of_resource_identity_switch():
    config = obs.TelemetryConfig.from_environment(CONFIG)
    assert config.agent_id == AGENT and CONFIG["AGENT_IDENTITY_ENABLED"] == "false"
    assert obs.TelemetryConfig.from_environment({"AGENT_OBSERVABILITY_MODE": "console"}).agent_id == ""


@pytest.mark.parametrize("name,value", [("AGENT_IDENTITY_APP_ID", BLUEPRINT), ("AZURE_CLIENT_ID", AGENT), ("AZURE_CLIENT_ID", BLUEPRINT)])
def test_blueprint_or_uami_never_masquerades_as_agent(name, value):
    with pytest.raises(ValueError, match="child Agent ID"):
        obs.TelemetryConfig.from_environment(dict(CONFIG, **{name: value}))


def resolver():
    credential = SimpleNamespace(tenant_id=TENANT, agent_app_id=AGENT, blueprint_app_id=BLUEPRINT,
                                 get_token=Mock(return_value=SimpleNamespace(token="offline-token", expires_on=10**11)))
    return obs.S2STokenResolver(obs.TelemetryConfig.from_environment(CONFIG), credential), credential


def test_s2s_resolver_uses_correct_audience_and_matching_identity():
    token_resolver, credential = resolver()
    assert token_resolver(AGENT, TENANT) == "offline-token"
    credential.get_token.assert_called_once_with(obs.OBSERVABILITY_SCOPE)
    credential.get_token.reset_mock()
    assert token_resolver(BLUEPRINT, TENANT) is None
    assert token_resolver(AGENT, BLUEPRINT) is None
    credential.get_token.assert_not_called()


def test_resolver_rejects_mismatched_credential():
    token_resolver, credential = resolver()
    credential.agent_app_id = UAMI
    with pytest.raises(ValueError, match="does not match"):
        obs.S2STokenResolver(token_resolver.config, credential)


def test_expired_token_and_authentication_failure_never_fallback_or_log_secrets(caplog):
    token_resolver, credential = resolver()
    credential.get_token.return_value.expires_on = 1
    assert token_resolver(AGENT, TENANT) is None
    credential.get_token.side_effect = RuntimeError("SECRET token & signed url")
    assert token_resolver(AGENT, TENANT) is None
    assert "SECRET" not in caplog.text


def test_invocation_tool_inference_are_correlated_without_content(pipeline):
    telemetry, exporter = pipeline
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4), text="SECRET response")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
    inbound = context.attach(baggage.set_baggage("user.email", "SECRET user"))
    try:
        with telemetry.invocation("mcp"), telemetry.tool("next_best_action"):
            result = telemetry.chat_completion(client, model="offline-model", messages=[{"content": "SECRET prompt"}])
            telemetry.approval_state("pending")
    finally:
        context.detach(inbound)
    assert result is response
    spans = exporter.get_finished_spans()
    assert [s.attributes["gen_ai.operation.name"] for s in spans] == ["chat", "execute_tool", "invoke_agent"]
    chat, tool, root = spans
    assert chat.parent.span_id == tool.context.span_id
    assert tool.parent.span_id == root.context.span_id and root.parent is None
    assert len({s.attributes["gen_ai.conversation.id"] for s in spans}) == 1
    assert chat.attributes["gen_ai.usage.input_tokens"] == 12
    assert tool.attributes["control_plane.approval.state"] == "pending"
    assert all(s.attributes["gen_ai.agent.id"] == AGENT and s.attributes["microsoft.tenant.id"] == TENANT for s in spans)
    assert "SECRET" not in " ".join(s.to_json() for s in spans)


def test_error_status_has_no_exception_message_stack_or_payload(pipeline):
    telemetry, exporter = pipeline
    with pytest.raises(RuntimeError, match="SECRET"):
        with telemetry.invocation("mcp"), telemetry.tool("hello_mcp"):
            raise RuntimeError("SECRET signed-url")
    assert obs._conversation.get() is None
    spans = exporter.get_finished_spans()
    assert all(span.status.status_code == StatusCode.ERROR and not span.events for span in spans)
    assert "SECRET" not in " ".join(span.to_json() for span in spans)


def test_status_failure_does_not_replace_application_exception(pipeline):
    telemetry, _ = pipeline
    bad_span = Mock()
    bad_span.set_attribute.side_effect = RuntimeError("SECRET exporter bug")
    obs.Observation(bad_span).failed()
    obs.Observation(bad_span).usage(SimpleNamespace(prompt_tokens=1))
    telemetry._tracer = Mock()
    telemetry._tracer.start_as_current_span.side_effect = RuntimeError("SECRET SDK bug")
    with pytest.raises(ValueError, match="original"):
        with telemetry.invocation("mcp"):
            raise ValueError("original")


def test_concurrent_invocations_do_not_share_identity_context(pipeline):
    telemetry, exporter = pipeline

    async def run():
        ready, finish = asyncio.Event(), asyncio.Event()

        async def first():
            with telemetry.invocation("mcp"):
                ready.set()
                await finish.wait()
                with telemetry.tool("first"):
                    pass

        async def second():
            await ready.wait()
            with telemetry.invocation("web"), telemetry.tool("second"):
                finish.set()

        await asyncio.gather(first(), second())

    asyncio.run(run())
    roots = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent"]
    assert len(roots) == 2
    assert len({s.attributes["gen_ai.conversation.id"] for s in roots}) == 2
    assert all(s.parent is None for s in roots)


def test_shutdown_flushes_provider_before_closing_credential(pipeline):
    telemetry, _ = pipeline
    calls = []
    telemetry._provider = Mock(shutdown=Mock(side_effect=lambda: calls.append("provider")))
    telemetry._credential = Mock(close=Mock(side_effect=lambda: calls.append("credential")))
    telemetry.shutdown()
    telemetry.shutdown()
    assert calls == ["provider", "credential"]


@pytest.mark.parametrize("is_error", [False, True])
def test_real_mcp_wrapper_records_only_catalog_name_and_root_status(pipeline, is_error):
    telemetry, exporter = pipeline
    source = ast.parse((ROOT / "src/next_best_action_agent.py").read_text(encoding="utf-8"))
    nodes = [node for node in source.body if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef))
             and node.name in {"MCPToolResult", "execute_tool"}]
    result = SimpleNamespace(isError=is_error, content=[{"text": "SECRET result"}])
    ns = {"Dict": dict, "Any": object, "List": list, "dataclass": dataclass,
          "time": time, "telemetry": telemetry, "TOOLS": [SimpleNamespace(name="next_best_action")],
          "_execute_tool_impl": AsyncMock(return_value=result), "logger": Mock(), "episode_capture": None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "mcp-telemetry-test", "exec"), ns)
    returned = asyncio.run(ns["execute_tool"]("SECRET unknown name", {"task": "SECRET prompt"}))
    assert returned is result
    spans = exporter.get_finished_spans()
    assert spans[0].attributes["gen_ai.tool.name"] == "unknown"
    assert all((s.status.status_code == StatusCode.ERROR) == is_error for s in spans)
    assert "SECRET" not in " ".join(s.to_json() for s in spans)


@pytest.mark.parametrize("http_status", [200, 403])
def test_real_agent365_sdk_export_uses_pinned_s2s_identity_and_safe_diagnostics(http_status):
    code = '''
import json, socket
from types import SimpleNamespace
from unittest.mock import Mock
import requests
requests.Session.send = Mock(side_effect=AssertionError("NETWORK NOT ALLOWED"))
socket.socket.connect = Mock(side_effect=AssertionError("NETWORK NOT ALLOWED"))
from src.agent_observability import AgentTelemetry, OBSERVABILITY_SCOPE
CONFIG = ''' + repr(CONFIG) + '''
calls = []
def post(self, url, **kwargs):
    calls.append((url, kwargs))
    return SimpleNamespace(status_code=''' + str(http_status) + ''', text="SECRET response echoed by service", headers={"www-authenticate":"SECRET header"})
requests.Session.post = post
credential = SimpleNamespace(tenant_id=CONFIG["AZURE_TENANT_ID"], agent_app_id=CONFIG["AGENT_IDENTITY_APP_ID"],
    blueprint_app_id=CONFIG["AGENT_IDENTITY_BLUEPRINT_APP_ID"], close=Mock(),
    get_token=Mock(return_value=SimpleNamespace(token="OFFLINE_TOKEN", expires_on=10**11)))
t = AgentTelemetry()
t.configure(environ=CONFIG, credential_factory=lambda **kwargs: credential)
from opentelemetry import baggage, context
incoming = context.attach(baggage.set_baggage("user.email", "SECRET inbound email"))
try:
    with t.invocation("mcp"), t.tool("hello_mcp"):
        pass
finally:
    context.detach(incoming)
t.shutdown()
assert calls, "SDK did not configure an exporter"
for url, options in calls:
    assert url == "https://agent365.svc.cloud.microsoft/observabilityService/tenants/" + CONFIG["AZURE_TENANT_ID"] + "/otlp/agents/" + CONFIG["AGENT_IDENTITY_APP_ID"] + "/traces?api-version=1"
    assert options["headers"]["authorization"] == "Bearer OFFLINE_TOKEN"
    body=options["data"].decode()
    assert "SECRET" not in body and "OFFLINE_TOKEN" not in body
    assert "invoke_agent" in body and "execute_tool" in body
credential.get_token.assert_called_with(OBSERVABILITY_SCOPE)
credential.close.assert_called_once()
requests.Session.send.assert_not_called()
socket.socket.connect.assert_not_called()
print("OFFLINE_EXPORT_OK")
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "A365_", "AGENT_IDENTITY_", "APPLICATIONINSIGHTS_"))}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "OFFLINE_EXPORT_OK" in result.stdout
    assert "SECRET" not in result.stdout + result.stderr
    assert "OFFLINE_TOKEN" not in result.stdout + result.stderr


def test_console_mode_real_distro_no_network_even_with_stale_export_flag():
    # A fresh process is required: OpenTelemetry's global provider is set once.
    code = '''
import json, socket
from unittest.mock import Mock
import requests
requests.Session.send = Mock(side_effect=AssertionError("NETWORK NOT ALLOWED"))
socket.socket.connect = Mock(side_effect=AssertionError("NETWORK NOT ALLOWED"))
from src.agent_observability import AgentTelemetry
t = AgentTelemetry()
t.configure(environ={"AGENT_OBSERVABILITY_MODE":"console"})
t.configure(environ={"AGENT_OBSERVABILITY_MODE":"console"})
with t.invocation("mcp"), t.tool("hello_mcp"):
    pass
t.shutdown()
requests.Session.send.assert_not_called()
socket.socket.connect.assert_not_called()
print("OFFLINE_OK")
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "A365_", "AGENT_IDENTITY_", "APPLICATIONINSIGHTS_"))}
    env["ENABLE_A365_OBSERVABILITY_EXPORTER"] = "true"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "OFFLINE_OK" in result.stdout
    assert '"gen_ai.operation.name": "invoke_agent"' in result.stdout
    assert '"gen_ai.agent.id"' not in result.stdout
    assert '"microsoft.tenant.id"' not in result.stdout


def test_wiring_manual_calls_only_preserves_pinned_framework():
    source = (ROOT / "src/next_best_action_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    completion_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "chat_completion"]
    assert len(completion_calls) == 5
    assert "client.chat.completions.create(" not in source
    assert "await asyncio.to_thread(telemetry.configure)" in source
    assert "await asyncio.to_thread(telemetry.shutdown)" in source
    assert 'with telemetry.invocation("web")' in source
    assert "AgentFrameworkInstrumentor" not in source
    requirements = (ROOT / "src/requirements.txt").read_text()
    assert "microsoft-opentelemetry==1.3.9" in requirements
    assert "agent-framework-core==1.0.0b260107" in requirements