"""Inject approval configuration without persisting or printing a signed URL.

Requires Python 3.10+, an existing Azure CLI login, and kubectl already pointing
at the intended cluster with its namespace provisioned. No login, role grant,
namespace creation or rollout is performed here. The caller owns rollout.

Configuration precedence: explicit non-secret arguments > process environment
> optional --from-azd values (JSON captured in memory). No environment files are
read or written directly. --check-only checks syntax/completeness, not deployed
resources or permissions, and never retrieves the webhook or writes resources.
Only --apply retrieves listCallbackUrl and applies the Secret and ConfigMap.
Within each source, APPROVAL_RUNTIME_CONFIG (object or JSON string) supersedes
legacy flat values, avoiding stale generated IDs after a parent redeployment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import NoReturn
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID


OPTION_ENV = {
    "subscription-id": "AZURE_SUBSCRIPTION_ID",
    "resource-group": "AZURE_RESOURCE_GROUP_NAME",
    "logic-app-name": "APPROVAL_LOGIC_APP_NAME",
    "namespace": "K8S_NAMESPACE",
    "tenant-id": "AZURE_TENANT_ID",
    "approver-tenant-id": "APPROVAL_APPROVER_TENANT_ID",
    "callback-audience": "APPROVAL_CALLBACK_AUDIENCE",
    "callback-principal-id": "APPROVAL_CALLBACK_PRINCIPAL_ID",
    "approver-ids": "APPROVAL_APPROVER_IDS",
    "approvals-container": "COSMOSDB_APPROVALS_CONTAINER",
    "timeout-hours": "APPROVAL_TIMEOUT_HOURS",
    "callback-url": "APPROVAL_CALLBACK_URL",
    "environment": "DEPLOYMENT_ENVIRONMENT",
    "cluster-name": "AKS_CLUSTER_NAME",
}
REQUIRED_KEYS = tuple(OPTION_ENV.values())
RUNTIME_KEYS = (
    "AZURE_TENANT_ID", "APPROVAL_APPROVER_TENANT_ID", "APPROVAL_CALLBACK_AUDIENCE",
    "APPROVAL_CALLBACK_PRINCIPAL_ID", "APPROVAL_APPROVER_IDS",
    "COSMOSDB_APPROVALS_CONTAINER", "APPROVAL_TIMEOUT_HOURS",
    "APPROVAL_CALLBACK_URL", "DEPLOYMENT_ENVIRONMENT", "AKS_CLUSTER_NAME",
)
GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
CALLBACK_URL = re.compile(
    r"https://[a-zA-Z0-9][a-zA-Z0-9.-]*(?::443)?/agent-approvals/callback"
)


class ConfigurationError(Exception):
    """Only fixed diagnostic text and allowlisted setting names may be exposed."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse normally repeats invalid input, which could contain a secret.
        self.exit(2, "Invalid arguments; select --check-only or --apply (see --help).\n")


def _invalid(name: str) -> NoReturn:
    raise ConfigurationError(f"Invalid configuration: {name}.")


def _guid(value: str, name: str) -> str:
    if not GUID.fullmatch(value) or UUID(value).int == 0:
        _invalid(name)
    return value.lower()


def validate_configuration(values: dict[str, str]) -> dict[str, str]:
    """Validate every required value before any secret retrieval or mutation."""
    config: dict[str, str] = {}
    missing = []
    invalid = []
    for name in REQUIRED_KEYS:
        value = values.get(name, "")
        if not isinstance(value, str):
            invalid.append(name)
        elif not value.strip():
            missing.append(name)
        else:
            config[name] = value.strip()
    if missing:
        raise ConfigurationError("Missing configuration: " + ", ".join(missing) + ".")
    if invalid:
        raise ConfigurationError("Invalid configuration: " + ", ".join(invalid) + ".")

    for name in ("AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "APPROVAL_APPROVER_TENANT_ID", "APPROVAL_CALLBACK_PRINCIPAL_ID"):
        config[name] = _guid(config[name], name)
    audience = config["APPROVAL_CALLBACK_AUDIENCE"]
    if not audience.startswith("api://"):
        _invalid("APPROVAL_CALLBACK_AUDIENCE")
    config["APPROVAL_CALLBACK_AUDIENCE"] = "api://" + _guid(audience[6:], "APPROVAL_CALLBACK_AUDIENCE")

    approvers = config["APPROVAL_APPROVER_IDS"].split(",")
    if not 1 <= len(approvers) <= 100:
        _invalid("APPROVAL_APPROVER_IDS")
    # Do not drop empty/malformed entries or fall back to a sponsor or caller.
    config["APPROVAL_APPROVER_IDS"] = ",".join(_guid(item.strip(), "APPROVAL_APPROVER_IDS") for item in approvers)
    if not re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-4])", config["APPROVAL_TIMEOUT_HOURS"]):
        _invalid("APPROVAL_TIMEOUT_HOURS")
    callback = config["APPROVAL_CALLBACK_URL"]
    if len(callback) > 2048 or not CALLBACK_URL.fullmatch(callback):
        _invalid("APPROVAL_CALLBACK_URL")

    patterns = {
        "AZURE_RESOURCE_GROUP_NAME": r"[A-Za-z0-9_().-]{1,90}",
        "APPROVAL_LOGIC_APP_NAME": r"[A-Za-z0-9][A-Za-z0-9_().-]{0,79}",
        "K8S_NAMESPACE": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        "COSMOSDB_APPROVALS_CONTAINER": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}",
        "DEPLOYMENT_ENVIRONMENT": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
        "AKS_CLUSTER_NAME": r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?",
    }
    for name, pattern in patterns.items():
        if not re.fullmatch(pattern, config[name]):
            _invalid(name)
    if config["AZURE_RESOURCE_GROUP_NAME"].endswith("."):
        _invalid("AZURE_RESOURCE_GROUP_NAME")
    return config


def _run(command: list[str], *, input_text: str = "") -> str:
    """Capture stdout, discard stderr, close stdin to prompts; never echo errors."""
    executable = shutil.which(command[0])
    if executable is None:
        raise ConfigurationError(f"Required CLI unavailable: {command[0]}; install/configure it separately.")
    child_env = dict(os.environ)
    for name in ("LOGIC_APP_APPROVAL_WEBHOOK", "APPROVAL_LOGIC_APP_TRIGGER_URL"):
        child_env.pop(name, None)
    child_env.update({
        "AZURE_LOGGING_ENABLE_LOG_FILE": "false",
        "AZURE_CORE_COLLECT_TELEMETRY": "false",
    })
    try:
        result = subprocess.run(
            [executable, *command[1:]],
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            check=False,
            shell=False,
            timeout=120,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ConfigurationError("Deployment command failed; output suppressed. Verify existing CLI access and context.") from None
    if result.returncode != 0:
        raise ConfigurationError("Deployment command failed; output suppressed. Verify existing CLI access and context.")
    return result.stdout


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid deployment command response; output suppressed.") from None


def load_configuration(args: argparse.Namespace) -> dict[str, str]:
    """Read only allowlisted settings; even a legacy trigger environment is ignored."""
    names = (*REQUIRED_KEYS, "AZURE_ENV_NAME")

    def select(source: dict) -> dict:
        selected = {name: source[name] for name in names if name in source}
        if "APPROVAL_RUNTIME_CONFIG" in source:
            grouped = source["APPROVAL_RUNTIME_CONFIG"]
            if isinstance(grouped, str):
                grouped = _json_object(grouped)
            if not isinstance(grouped, dict):
                raise ConfigurationError("Invalid APPROVAL_RUNTIME_CONFIG; expected a JSON object.")
            selected.update({name: grouped[name] for name in names if name in grouped})
        return selected

    values = {}
    if args.from_azd:
        raw = _json_object(_run(["azd", "env", "get-values", "--output", "json"]))
        values.update(select(raw))
    values.update(select(dict(os.environ)))
    values.update({name: getattr(args, name) for name in REQUIRED_KEYS if getattr(args, name) is not None})
    if type(values.get("APPROVAL_TIMEOUT_HOURS")) is int:
        values["APPROVAL_TIMEOUT_HOURS"] = str(values["APPROVAL_TIMEOUT_HOURS"])
    # Explicit empty overrides stay empty and fail validation, rather than using
    # a stale azd value. Only absence permits the documented defaults/alias.
    values.setdefault("K8S_NAMESPACE", "mcp-agents")
    values.setdefault("APPROVAL_APPROVER_TENANT_ID", values.get("AZURE_TENANT_ID", ""))
    if "DEPLOYMENT_ENVIRONMENT" not in values and "AZURE_ENV_NAME" in values:
        values["DEPLOYMENT_ENVIRONMENT"] = values["AZURE_ENV_NAME"]
    return {name: values.get(name, "") for name in REQUIRED_KEYS}


def _get_webhook(config: dict[str, str]) -> str:
    resource_id = (
        f"/subscriptions/{config['AZURE_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{quote(config['AZURE_RESOURCE_GROUP_NAME'], safe='')}"
        f"/providers/Microsoft.Logic/workflows/{quote(config['APPROVAL_LOGIC_APP_NAME'], safe='')}"
    )
    url = (
        f"https://management.azure.com{resource_id}"
        "/triggers/When_an_HTTP_request_is_received/listCallbackUrl?api-version=2019-05-01"
    )
    response = _json_object(_run([
        "az", "rest", "--method", "POST", "--url", url,
        "--subscription", config["AZURE_SUBSCRIPTION_ID"], "--output", "json", "--only-show-errors",
    ]))
    webhook = response.get("value")
    try:
        if not isinstance(webhook, str) or len(webhook) > 8192:
            raise ValueError
        parts = urlsplit(webhook)
        if (
            parts.scheme != "https" or not parts.hostname
            or not parts.hostname.endswith(".logic.azure.com")
            or parts.port not in (None, 443) or parts.username is not None
            or parts.password is not None or "#" in webhook or "\\" in webhook
            or any(c.isspace() or ord(c) < 32 for c in webhook)
            or not re.fullmatch(r"/workflows/[A-Za-z0-9-]+/triggers/When_an_HTTP_request_is_received/paths/invoke", parts.path)
        ):
            raise ValueError
        signatures = parse_qs(parts.query).get("sig", [])
        if len(signatures) != 1 or not signatures[0]:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid Logic App trigger response; output suppressed.") from None
    return webhook


def configure_runtime(values: dict[str, str]) -> None:
    config = validate_configuration(values)
    webhook = _get_webhook(config)
    namespace = config["K8S_NAMESPACE"]
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "mcp-approval-secrets", "namespace": namespace},
        "type": "Opaque",
        "stringData": {"LOGIC_APP_APPROVAL_WEBHOOK": webhook},
    }
    config_map = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "mcp-approval-config", "namespace": namespace},
        "data": {name: config[name] for name in RUNTIME_KEYS},
    }
    # Server-side apply avoids copying the plaintext stringData into a client-side
    # last-applied annotation. Conflicting field ownership fails; never force it.
    # Neither resource nor the signed URL goes through argv, files or os.environ.
    for resource in (secret, config_map):
        _run([
            "kubectl", "apply", "--server-side", "--field-manager=mcp-approval-runtime",
            "--namespace", namespace, "-f", "-",
        ], input_text=json.dumps(resource))


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("Approval runtime configuration requires Python 3.10+; configure an interpreter separately.", file=sys.stderr)
        return 2
    parser = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true", help="Validate configuration only; do not fetch the webhook or mutate resources.")
    mode.add_argument("--apply", action="store_true", help="Fetch the webhook in memory and apply the Secret and ConfigMap.")
    parser.add_argument("--from-azd", action="store_true", help="Read lower-priority azd environment values as JSON in memory.")
    for option, name in OPTION_ENV.items():
        parser.add_argument(f"--{option}", dest=name, help=f"Non-secret setting; defaults to {name}.")
    args = parser.parse_args(argv)
    try:
        values = load_configuration(args)
        if args.check_only:
            validate_configuration(values)
            print("Approval configuration is valid; no webhook retrieved or resources changed.")
        else:
            configure_runtime(values)
            print("Approval Secret and ConfigMap applied; the caller must roll out the deployment.")
        return 0
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        # Do not expose subprocess exceptions, JSON bodies or a traceback that
        # could contain a signed trigger URL, even on unexpected failures.
        print("Approval runtime configuration failed; details suppressed.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())