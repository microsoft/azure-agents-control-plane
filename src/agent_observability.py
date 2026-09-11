"""Opt-in, metadata-only tracing with the Microsoft OpenTelemetry distro.

No registration, Graph calls, consent or identity creation. Agent 365 export
uses an existing Entra Agent ID and its own secretless S2S credential; Azure
data-plane clients may continue using the UAMI. Console mode needs no identity.
An initialized exporter is NOT evidence that a tenant accepted the telemetry.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import entry_points
import logging
import os
import re
from threading import Lock
import time
from typing import Mapping
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)
OBSERVABILITY_SCOPE = "api://9b975845-388f-4429-889e-eab1ef63949c/.default"
MODES = ("off", "console", "agent365")
_conversation: ContextVar[str | None] = ContextVar("agent_telemetry_conversation", default=None)


def _guid(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) or not UUID(value).int:
        raise ValueError(name + " must contain an existing nonzero GUID for Agent 365 telemetry.")
    return str(UUID(value))


@dataclass(frozen=True)
class TelemetryConfig:
    mode: str = "off"
    tenant_id: str = ""
    agent_id: str = ""
    blueprint_id: str = ""

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> TelemetryConfig:
        mode = values.get("AGENT_OBSERVABILITY_MODE", "off").strip().lower()
        if mode not in MODES:
            raise ValueError("AGENT_OBSERVABILITY_MODE must be off, console or agent365.")
        if mode != "agent365":
            # Local traces deliberately have no invented Entra identity.
            return cls(mode=mode)
        tenant, agent, blueprint = (_guid(values, name) for name in (
            "AZURE_TENANT_ID", "AGENT_IDENTITY_APP_ID", "AGENT_IDENTITY_BLUEPRINT_APP_ID",
        ))
        if agent == blueprint or values.get("AZURE_CLIENT_ID", "").lower() in (agent, blueprint):
            raise ValueError("Telemetry requires a child Agent ID, not its blueprint or bootstrap UAMI.")
        return cls(mode=mode, tenant_id=tenant, agent_id=agent, blueprint_id=blueprint)


class S2STokenResolver:
    """Borrow a pinned AgentIdentityCredential; never substitute a user/UAMI token."""

    def __init__(self, config: TelemetryConfig, credential):
        if (credential.tenant_id, credential.agent_app_id, credential.blueprint_app_id) != (
            config.tenant_id, config.agent_id, config.blueprint_id,
        ):
            raise ValueError("Telemetry credential does not match the configured agent and tenant.")
        self.config, self.credential = config, credential

    def __call__(self, agent_id: str, tenant_id: str) -> str | None:
        if (agent_id, tenant_id) != (self.config.agent_id, self.config.tenant_id):
            return None
        try:
            token = self.credential.get_token(OBSERVABILITY_SCOPE)
            if isinstance(token.token, str) and token.token and token.expires_on > time.time() + 30:
                return token.token
        except Exception:
            # Authentication errors can contain credentials. Do not log them.
            pass
        logger.warning("Agent 365 telemetry token unavailable; export cannot authenticate.")
        return None


class _SafeSdkDiagnostics(logging.Filter):
    """The vendor exporter may log raw HTTP responses/headers on failure."""

    def __init__(self):
        super().__init__()
        self.initialization_failed = False

    def filter(self, record):
        if record.name == "microsoft.opentelemetry._distro" and record.levelno >= logging.ERROR:
            self.initialization_failed = True
        if record.levelno < logging.WARNING:
            return False
        # Keep only a numeric HTTP status, never response/credential material.
        status = record.args[0] if isinstance(record.args, tuple) and record.args else None
        if isinstance(record.msg, str) and record.msg.startswith("HTTP ") and type(status) is int and 100 <= status <= 599:
            record.msg, record.args = "Agent 365 telemetry HTTP %d; response details suppressed.", (status,)
        else:
            record.msg, record.args = "Agent 365 telemetry SDK diagnostic; details suppressed.", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


class Observation:
    def __init__(self, span=None):
        self.span = span

    def failed(self):
        try:
            if self.span is not None:
                from opentelemetry.trace import StatusCode
                self.span.set_attribute("error.type", "operation_failed")
                self.span.set_status(StatusCode.ERROR)
        except Exception:
            logger.warning("Telemetry status could not be recorded.")

    def usage(self, usage):
        try:
            if self.span is not None and usage is not None:
                for field, attribute in (("prompt_tokens", "gen_ai.usage.input_tokens"), ("completion_tokens", "gen_ai.usage.output_tokens")):
                    value = getattr(usage, field, None)
                    if type(value) is int and value >= 0:
                        self.span.set_attribute(attribute, value)
        except Exception:
            logger.warning("Telemetry token usage could not be recorded.")


class AgentTelemetry:
    """One process-owned pipeline, lazy imports, no content auto-instrumentation."""

    def __init__(self):
        self.config = TelemetryConfig()
        self._configured = False
        self._lock = Lock()
        self._provider = None
        self._tracer = None
        self._credential = None

    def configure(self, *, environ=None, credential_factory=None):
        values = os.environ if environ is None else environ
        config = TelemetryConfig.from_environment(values)
        with self._lock:
            if self._configured:
                if config != self.config:
                    raise ValueError("Restart the service to change observability mode or identity.")
                return
            if config.mode == "off":
                self.config, self._configured = config, True
                return
            # The distro auto-enables OTLP from process settings. Do not silently
            # export to another destination, even in local console mode.
            if any(value and name.startswith("OTEL_EXPORTER_OTLP") and name.endswith("ENDPOINT") for name, value in os.environ.items()):
                raise ValueError("Remove OTLP endpoint settings before using this Agent 365/console-only integration.")
            if any(os.environ.get(name) for name in ("A365_OBSERVABILITY_DOMAIN_OVERRIDE", "A365_OBSERVABILITY_SCOPE_OVERRIDE")):
                raise ValueError("Agent 365 telemetry endpoint/scope overrides are not supported.")
            # Disable SDK diagnostics export and remote configuration fetches
            # before importing the distro, including in console-only mode.
            os.environ["APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"] = "true"
            os.environ["MICROSOFT_OTEL_SDKSTATS_DISABLED"] = "true"
            os.environ["APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED"] = "true"
            from microsoft.opentelemetry import use_microsoft_opentelemetry
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider

            if isinstance(trace.get_tracer_provider(), TracerProvider):
                raise ValueError("An OpenTelemetry pipeline is already configured; do not initialize a second one.")
            diagnostics = _SafeSdkDiagnostics()
            for name in ("microsoft.opentelemetry._distro", "microsoft.opentelemetry.a365.core.exporters.agent365_exporter"):
                logging.getLogger(name).addFilter(diagnostics)
            resolver = None
            if config.mode == "agent365":
                if credential_factory is None:
                    if __package__:
                        from .agent_identity import AgentIdentityCredential
                    else:
                        from agent_identity import AgentIdentityCredential
                    credential_factory = AgentIdentityCredential
                self._credential = credential_factory(
                    tenant_id=config.tenant_id, blueprint_app_id=config.blueprint_id, agent_app_id=config.agent_id,
                )
                resolver = S2STokenResolver(config, self._credential)
            try:
                use_microsoft_opentelemetry(
                    # False here guarantees console mode cannot be promoted to
                    # remote export by ENABLE_A365_OBSERVABILITY_EXPORTER.
                    enable_a365=config.mode == "agent365",
                    a365_enable_observability_exporter=config.mode == "agent365",
                    a365_token_resolver=resolver,
                    a365_cluster_category="prod", a365_use_s2s_endpoint=True,
                    a365_suppress_invoke_agent_input=True,
                    a365_exporter_disable_offline_storage=True,
                    a365_max_queue_size=256, a365_max_export_batch_size=32,
                    enable_console=config.mode == "console", enable_azure_monitor=False,
                    disable_logging=True, disable_metrics=True, enable_sensitive_data=False,
                    resource=Resource({"service.name": "next-best-action", "service.namespace": "azure-agents-control-plane"}),
                    instrumentation_options={ep.name: {"enabled": False} for ep in entry_points(group="opentelemetry_instrumentor")},
                )
                self._provider = trace.get_tracer_provider()
                if diagnostics.initialization_failed:
                    raise RuntimeError("The SDK could not initialize its exporter.")
                self._tracer = self._provider.get_tracer("azure-agents-control-plane")
                self.config, self._configured = config, True
            except Exception:
                if self._provider is not None:
                    self._provider.shutdown()
                    self._provider = None
                if self._credential is not None:
                    self._credential.close()
                    self._credential = None
                raise RuntimeError("Observability initialization failed; diagnostics suppressed.") from None

    @contextmanager
    def _operation(self, operation: str, **attributes):
        if self._tracer is None:
            yield Observation()
            return
        from opentelemetry import trace
        from opentelemetry.context import Context

        metadata = {
            "gen_ai.operation.name": operation,
            "gen_ai.agent.name": "Next Best Action",
            "gen_ai.conversation.id": _conversation.get() or str(uuid4()),
            **attributes,
        }
        if self.config.mode == "agent365":
            metadata.update({"microsoft.tenant.id": self.config.tenant_id, "gen_ai.agent.id": self.config.agent_id,
                             "microsoft.a365.agent.blueprint.id": self.config.blueprint_id})
        # Keep only this invocation's parent span, never inbound baggage/PII or
        # an untrusted tenant/agent claim. All identities are server configured.
        # The vendor processor treats an empty Context as false and falls back
        # to the ambient one. A nonempty context with INVALID_SPAN still creates
        # a root trace, without accidentally inheriting ambient user baggage.
        parent_span = trace.get_current_span() if operation != "invoke_agent" else trace.INVALID_SPAN
        parent = trace.set_span_in_context(parent_span, Context())
        try:
            manager = self._tracer.start_as_current_span(
                operation, context=parent, attributes=metadata,
                record_exception=False, set_status_on_exception=False,
            )
            observation = Observation(manager.__enter__())
        except Exception:
            logger.warning("Telemetry span unavailable; application authorization is unchanged.")
            yield Observation()
            return
        try:
            yield observation
        except BaseException:
            observation.failed()
            raise
        finally:
            # Do not hand the SDK raw exceptions, prompts or tool responses.
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.warning("Telemetry span finalization failed; diagnostics suppressed.")

    @contextmanager
    def invocation(self, channel: str):
        if self._tracer is None or _conversation.get() is not None:
            yield Observation()
            return
        token = _conversation.set(str(uuid4()))
        try:
            with self._operation("invoke_agent", **{"microsoft.channel.name": channel}) as observation:
                yield observation
        finally:
            _conversation.reset(token)

    def tool(self, name: str):
        # Caller supplies a name from the server's tool catalog, never arguments.
        return self._operation("execute_tool", **{"gen_ai.tool.name": name, "gen_ai.tool.type": "function", "gen_ai.tool.call.id": str(uuid4())})

    def chat_completion(self, client, *, model: str, messages):
        with self.invocation("internal"):
            with self._operation("chat", **{"gen_ai.request.model": model, "gen_ai.provider.name": "azure.ai.openai"}) as observation:
                response = client.chat.completions.create(model=model, messages=messages)
                observation.usage(getattr(response, "usage", None))
                return response

    def approval_state(self, state: str):
        if self._tracer is not None and state in {"pending", "approved", "rejected", "timeout", "error"}:
            try:
                from opentelemetry import trace
                trace.get_current_span().set_attribute("control_plane.approval.state", state)
            except Exception:
                logger.warning("Telemetry approval state could not be recorded.")

    def shutdown(self):
        with self._lock:
            self._tracer = None
            try:
                if self._provider is not None:
                    self._provider.shutdown()
                    self._provider = None
            except Exception:
                logger.warning("Telemetry shutdown failed; diagnostics suppressed.")
            finally:
                if self._credential is not None:
                    try:
                        self._credential.close()
                    except Exception:
                        logger.warning("Telemetry credential cleanup failed; diagnostics suppressed.")
                    finally:
                        self._credential = None


telemetry = AgentTelemetry()