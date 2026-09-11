"""Gated build/push with no ACR firewall, bypass, consent, or role changes.

ACR_BUILD_MODE=acr-task uses an existing ACR_TASK_AGENT_POOL for a restricted
registry. The pool must be running, Linux and VNet-attached; the submitting
runner must also have private connectivity for context upload/log retrieval.
ACR_BUILD_MODE=docker uses Docker on an already-connected runner. Neither mode
creates private connectivity. --check-only never logs into Docker or builds.
Shell wrappers read authoritative azd identity settings. Only build options,
not identity flags/IDs, may be overridden by the process environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.deployment_gate import (  # noqa: E402
    GateBlocked, RegistryError, SafeParser, check_deployment, flag, json_object,
    load_configuration, nonempty, nonzero_guid, run_cli,
)

BUILD_OVERRIDES = ("CONTAINER_REGISTRY", "ACR_NAME", "IMAGE_NAME", "IMAGE_TAG", "MCP_AGENT_RUNTIME", "ACR_BUILD_MODE", "ACR_TASK_AGENT_POOL")


def network_policy(registry: dict) -> dict:
    access = registry.get("publicNetworkAccess")
    rules = registry.get("networkRuleSet") or {}
    if access not in ("Disabled", "Enabled") or not isinstance(rules, dict):
        raise RegistryError("network_unverified", "Cannot verify ACR network policy; no build is permitted.")
    if rules.get("defaultAction", "Allow") not in ("Allow", "Deny"):
        raise RegistryError("network_unverified", "Cannot verify ACR firewall default action.")
    return {"publicNetworkAccess": access, "networkRuleSet": rules,
            "networkRuleBypassOptions": registry.get("networkRuleBypassOptions"),
            "privateEndpointConnections": registry.get("privateEndpointConnections", [])}


def build_and_push(values: dict[str, str], *, check_only: bool = False) -> dict:
    # Before registry authentication, Docker, or ACR Tasks. Never a cached gate.
    check_deployment(values)
    subscription = nonzero_guid(nonempty(values, "AZURE_SUBSCRIPTION_ID"), "Subscription ID")
    server = nonempty(values, "CONTAINER_REGISTRY").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{3,62}\.azurecr\.io", server):
        raise RegistryError("invalid_configuration", "CONTAINER_REGISTRY must be an Azure public-cloud ACR login server.")
    name = values.get("ACR_NAME") or server.split(".", 1)[0]
    if not re.fullmatch(r"[a-zA-Z0-9]{5,50}", name):
        raise RegistryError("invalid_configuration", "Hashed login servers require the resource name in ACR_NAME.")
    image = values.get("IMAGE_NAME") or "mcp-agents"
    if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", image):
        raise RegistryError("invalid_configuration", "Invalid IMAGE_NAME.")
    runtime = (values.get("MCP_AGENT_RUNTIME") or "python").lower()
    if runtime not in ("python", "typescript"):
        raise RegistryError("invalid_configuration", "MCP_AGENT_RUNTIME must be python or typescript.")
    tag = values.get("IMAGE_TAG") or ("latest" if runtime == "python" else "typescript")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise RegistryError("invalid_configuration", "Invalid IMAGE_TAG.")
    agent_integration = flag(values, "AGENT_IDENTITY_ENABLED") or flag(values, "AGENT_REGISTRY_ENABLED") or values.get("AGENT_OBSERVABILITY_MODE", "off") != "off"
    if agent_integration and tag in ("latest", "typescript"):
        raise RegistryError("invalid_configuration", "Agent ID, registry and telemetry deployments require an explicit versioned IMAGE_TAG, not a moving default tag.")
    mode = values.get("ACR_BUILD_MODE") or "acr-task"
    if mode not in ("acr-task", "docker"):
        raise RegistryError("invalid_configuration", "ACR_BUILD_MODE must be acr-task or docker.")
    show = ["az", "acr", "show", "--name", name, "--subscription", subscription, "--output", "json", "--only-show-errors"]
    registry = json_object(run_cli(show))
    if registry.get("loginServer", "").lower() != server or not registry.get("id", "").lower().startswith(f"/subscriptions/{subscription}/resourcegroups/"):
        raise RegistryError("registry_mismatch", "Resolved ACR does not match the requested subscription/login server.")
    before = network_policy(registry)
    restricted = before["publicNetworkAccess"] == "Disabled" or before["networkRuleSet"].get("defaultAction") == "Deny"
    pool = values.get("ACR_TASK_AGENT_POOL", "")
    pool_args = []
    if mode == "acr-task":
        if restricted and not pool:
            raise RegistryError("private_builder_required", "Private/restricted ACR requires ACR_TASK_AGENT_POOL or ACR_BUILD_MODE=docker on a connected runner. Network policy was not changed.")
        if pool:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}", pool):
                raise RegistryError("invalid_configuration", "Invalid ACR_TASK_AGENT_POOL.")
            record = json_object(run_cli(["az", "acr", "agentpool", "show", "--registry", name, "--name", pool, "--subscription", subscription, "--output", "json", "--only-show-errors"]))
            if record.get("provisioningState") != "Succeeded" or record.get("os") != "Linux" or type(record.get("count")) is not int or record["count"] < 1:
                raise RegistryError("private_builder_unavailable", "The selected ACR pool must be a provisioned Linux pool with at least one instance.")
            if restricted and not re.fullmatch(r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Network/virtualNetworks/[^/]+/subnets/[^/]+", record.get("virtualNetworkSubnetResourceId", ""), re.IGNORECASE):
                raise RegistryError("private_builder_unavailable", "The selected ACR pool is not VNet-attached; private connectivity is required.")
            pool_args = ["--agent-pool", pool]
    full_image = f"{server}/{image}:{tag}"
    if check_only:
        return {"status": "satisfied", "readOnly": True, "mode": mode, "image": full_image,
                "privateRegistry": before["publicNetworkAccess"] == "Disabled",
                "note": "Configuration/consent checked; actual private DNS, connectivity, package feeds and build authorization are exercised only by the build."}
    dockerfile = "Dockerfile" if runtime == "python" else "Dockerfile.typescript"
    try:
        if mode == "acr-task":
            run_cli(["az", "acr", "build", "--registry", name, "--subscription", subscription,
                     "--image", f"{image}:{tag}", "--platform", "linux/amd64",
                     "--file", dockerfile, *pool_args, str(ROOT / "src")], timeout=3600)
        else:
            run_cli(["az", "acr", "login", "--name", name, "--subscription", subscription, "--output", "none"])
            run_cli(["docker", "build", "--platform", "linux/amd64", "--file", str(ROOT / "src" / dockerfile), "--tag", full_image, str(ROOT / "src")], timeout=3600)
            run_cli(["docker", "push", full_image], timeout=1800)
        digest = run_cli(["az", "acr", "repository", "show", "--name", name, "--subscription", subscription,
                          "--image", f"{image}:{tag}", "--query", "digest", "--output", "tsv"]).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RegistryError("image_unverified", "The pushed image digest could not be verified.")
    finally:
        # Detect external drift but never overwrite another actor's policy.
        after = network_policy(json_object(run_cli(show)))
        if after != before:
            raise RegistryError("network_policy_changed", "ACR network policy changed during the build; rollout must stop. No automatic firewall rewrite was attempted.")
    return {"status": "built", "image": full_image, "digest": digest,
            "immutableImage": f"{server}/{image}@{digest}", "networkPolicyUnchanged": True}


def main(argv: list[str] | None = None) -> int:
    try:
        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--from-azd", action="store_true")
        parser.add_argument("--environment")
        parser.add_argument("--check-only", action="store_true")
        args = parser.parse_args(argv)
        values = load_configuration(from_azd=args.from_azd, environment=args.environment)
        if args.from_azd or args.environment:
            for key in BUILD_OVERRIDES:
                if os.environ.get(key):
                    values[key] = os.environ[key]
        print(json.dumps(build_and_push(values, check_only=args.check_only), sort_keys=True))
        return 0
    except GateBlocked as error:
        print(json.dumps(error.report, sort_keys=True))
        return 2
    except RegistryError as error:
        print(json.dumps({"status": "blocked", "error": error.as_dict()}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "inconclusive", "error": "Build failed; no automatic retry or network change was attempted."}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())