"""Opt-in, deployment-credential Agent Registry publisher.

Hook invocation (from the repository root, with deployment outputs exported):
    python scripts/publish_agent_registry.py --publish
Read-only publisher permission check, including before identity creation:
    python scripts/publish_agent_registry.py --preflight

AGENT_REGISTRY_ENABLED must explicitly be true (or 1) to publish. --publish does
not bypass the flag. --preflight deliberately works before opt-in or provisioning.
No az login, consent, deployment or runtime-agent authentication is performed here.

Required publication environment: AGENT_ENDPOINT_URL, AGENT_IDENTITY_APP_ID,
AGENT_REGISTRY_OWNER_IDS (comma-separated user/service-principal OBJECT IDs),
and AZURE_ENV_NAME (or explicit --source-agent-id and --state-file/--registry-id).
Defaults: AGENT_IDENTITY_DISPLAY_NAME, AGENT_REGISTRY_SOURCE_AGENT_ID (otherwise
azure-agents-control-plane:<AZURE_ENV_NAME>:next-best-action),
AGENT_REGISTRY_ORIGINATING_STORE (otherwise Azure Agents Control Plane).
AGENT_REGISTRY_API defaults to agent365; entra-beta is explicit compatibility.
AGENT_REGISTRY_ID adopts an existing registry ID (NOT an identity or package ID).
AGENT_REGISTRY_STATE_FILE overrides the durable .azure/<env>/agent-registry.json
journal. Preserve/share it across runners. No tokens or manifests are saved there.

Optional AGENT_REGISTRY_MANAGED_BY_APP_ID identifies the deployment managing app,
not the runtime agent. AGENT_IDENTITY_BLUEPRINT_OBJECT_ID is the blueprint *app
object* ID; do not substitute AGENT_IDENTITY_BLUEPRINT_APP_ID (the client ID).
AGENT_REGISTRY_CREATED_BY_ID, AGENT_REGISTRY_SOURCE_CREATED_AT and
AGENT_REGISTRY_SOURCE_MODIFIED_AT provide source provenance. If omitted, the
first source publication is attributed to the deployment caller and timestamped
then. Created provenance is never overwritten on subsequent publication.

Success stdout contains only identity/registry IDs. Informational notices go to
stderr; failures/preflight return safe JSON and a nonzero exit code when blocked.
The hook must propagate that exit code. No approvals/Teams settings are inferred.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_registry import (  # noqa: E402
    API_CHOICES, PREVIEW_NOTICE, AgentRegistryPublisher, GraphClient, RegistryError,
    guid, validate_publication,
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error text echoes arbitrary argument values.
        raise RegistryError("invalid_arguments", "Invalid command-line arguments; use --help. Values have been suppressed.")


def enabled_from_environment() -> bool:
    value = os.getenv("AGENT_REGISTRY_ENABLED", "false").strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise RegistryError("invalid_configuration", "AGENT_REGISTRY_ENABLED must be true/false or 1/0.")


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Read-only publisher permission check; no identity ID/endpoint needed.")
    mode.add_argument("--publish", action="store_true", help="Publish (the default); still requires AGENT_REGISTRY_ENABLED=true.")
    parser.add_argument("--api", choices=API_CHOICES, default=os.getenv("AGENT_REGISTRY_API", "agent365"))
    parser.add_argument("--endpoint", default=os.getenv("AGENT_ENDPOINT_URL"))
    parser.add_argument("--agent-identity-id", default=os.getenv("AGENT_IDENTITY_APP_ID"))
    parser.add_argument("--display-name", default=os.getenv("AGENT_IDENTITY_DISPLAY_NAME") or "Next Best Action Agent")
    parser.add_argument("--source-agent-id", default=os.getenv("AGENT_REGISTRY_SOURCE_AGENT_ID"))
    parser.add_argument("--originating-store", default=os.getenv("AGENT_REGISTRY_ORIGINATING_STORE", "Azure Agents Control Plane"))
    parser.add_argument("--owner-id", action="append", help="Repeat for each user/service-principal object ID; groups are not supported.")
    parser.add_argument("--managed-by-app-id", default=os.getenv("AGENT_REGISTRY_MANAGED_BY_APP_ID"))
    parser.add_argument("--blueprint-object-id", default=os.getenv("AGENT_IDENTITY_BLUEPRINT_OBJECT_ID"))
    parser.add_argument("--registry-id", default=os.getenv("AGENT_REGISTRY_ID"), help="Adopt a saved registry ID; never an Entra identity or package ID.")
    parser.add_argument("--tenant-id", default=os.getenv("AZURE_TENANT_ID"))
    parser.add_argument("--state-file", default=os.getenv("AGENT_REGISTRY_STATE_FILE"))
    parser.add_argument("--created-by-id", default=os.getenv("AGENT_REGISTRY_CREATED_BY_ID"))
    parser.add_argument("--source-created-at", default=os.getenv("AGENT_REGISTRY_SOURCE_CREATED_AT"))
    parser.add_argument("--source-modified-at", default=os.getenv("AGENT_REGISTRY_SOURCE_MODIFIED_AT"))
    parser.add_argument("--instance-manifest", default=str(ROOT / "agent365/manifests/agent_instance.json"))
    parser.add_argument("--card-manifest", default=str(ROOT / "agent365/manifests/agent_card_manifest.json"))
    return parser


def _template(path: str, values: dict[str, Any]) -> dict:
    try:
        file = Path(path)
        if file.stat().st_size > 128 * 1024:
            raise ValueError()
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RegistryError("invalid_template", "Cannot read a valid bounded JSON deployment template; file content/path is suppressed.") from None

    def resolve(value):
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if isinstance(value, str) and "${" in value:
            match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", value)
            if not match or match[1] not in values or values[match[1]] is None:
                raise RegistryError("invalid_template", "Template contains an unsupported or unresolved placeholder; arbitrary environment expansion is prohibited.")
            return values[match[1]]
        return value

    # Substitute decoded values, never text in raw JSON (avoids JSON injection).
    return resolve(document)


def publication_inputs(args) -> tuple[dict, dict, Path | None]:
    environment = os.getenv("AZURE_ENV_NAME")
    if environment and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", environment):
        raise RegistryError("invalid_configuration", "AZURE_ENV_NAME must be a safe environment name, not a path.")
    source_id = args.source_agent_id or (f"azure-agents-control-plane:{environment}:next-best-action" if environment else None)
    owner_values = args.owner_id
    if owner_values is None:
        owner_env = os.getenv("AGENT_REGISTRY_OWNER_IDS") or os.getenv("AGENT_REGISTRY_OWNER_ID") or os.getenv("OWNER_USER_ID", "")
        owner_values = [v.strip() for v in owner_env.split(",") if v.strip()]
    if not owner_values:
        raise RegistryError("invalid_configuration", "Supply --owner-id or AGENT_REGISTRY_OWNER_IDS with at least one user/service-principal object ID.")
    owners = [guid(v, "Owner object ID") for v in owner_values]
    values = {
        "AGENT_ENDPOINT_URL": args.endpoint,
        "AGENT_IDENTITY_APP_ID": args.agent_identity_id,
        "AGENT_IDENTITY_DISPLAY_NAME": args.display_name,
        "AGENT_REGISTRY_SOURCE_AGENT_ID": source_id,
        "AGENT_REGISTRY_ORIGINATING_STORE": args.originating_store,
        "AGENT_REGISTRY_OWNER_ID": owners[0],
    }
    instance = _template(args.instance_manifest, values)
    card = _template(args.card_manifest, values)
    if not isinstance(instance, dict) or not isinstance(card, dict):
        raise RegistryError("invalid_template", "Deployment templates must contain JSON objects.")
    instance.update(
        displayName=args.display_name, ownerIds=owners, sourceAgentId=source_id,
        originatingStore=args.originating_store, agentIdentityId=args.agent_identity_id,
    )
    card.update(name=args.display_name, url=args.endpoint)
    for key, value in (
        ("managedByAppId", args.managed_by_app_id), ("agentIdentityBlueprintId", args.blueprint_object_id),
        ("createdBy", args.created_by_id), ("sourceCreatedDateTime", args.source_created_at),
        ("sourceLastModifiedDateTime", args.source_modified_at),
    ):
        if value is not None:
            instance[key] = value
    instance, card = validate_publication(instance, card)
    state = Path(args.state_file).resolve() if args.state_file else (
        ROOT / ".azure" / environment / "agent-registry.json" if environment else None
    )
    if args.api == "agent365" and state is None and not args.registry_id:
        raise RegistryError("journal_required", "Provide AZURE_ENV_NAME, --state-file on durable storage, or an existing --registry-id.")
    return instance, card, state


def main(argv: list[str] | None = None) -> int:
    publisher = None
    instance = None
    try:
        args = _parser().parse_args(argv)
        enabled = enabled_from_environment()
        if not args.preflight and not enabled:
            print(json.dumps({"status": "skipped", "reason": "AGENT_REGISTRY_ENABLED is false"}))
            return 0
        if args.api not in API_CHOICES:
            raise RegistryError("invalid_configuration", "AGENT_REGISTRY_API must be agent365 or entra-beta.")
        if not args.preflight:
            instance, card, state = publication_inputs(args)
        print(PREVIEW_NOTICE, file=sys.stderr)
        with GraphClient(tenant_id=args.tenant_id) as graph:
            publisher = AgentRegistryPublisher(graph, api=args.api)
            if args.preflight:
                managed_by = guid(args.managed_by_app_id, "Managing application ID") if args.managed_by_app_id else None
                report = publisher.preflight(saved_id=args.registry_id, managed_by=managed_by)
                result = report
                exit_code = 0 if report["status"] == "satisfied" else (2 if report["status"] == "missing_permissions" else 3)
            else:
                result = publisher.publish(instance, card, saved_id=args.registry_id, journal_path=state)
                exit_code = 0
        print(json.dumps(result, sort_keys=True))
        return exit_code
    except RegistryError as error:
        failure: dict[str, Any] = {"status": "blocked", "error": error.as_dict()}
        if publisher and publisher.published_id:
            failure["agentRegistrationId" if publisher.api == "agent365" else "agentInstanceId"] = publisher.published_id
        if instance:
            failure["agentIdentityId"] = instance["agentIdentityId"]
        print(json.dumps(failure, sort_keys=True))
        return 2
    except Exception:
        # Includes credential, template and filesystem libraries: no repr/traceback
        # can accidentally print tokens, callback URLs, secrets or manifest values.
        print(json.dumps({"status": "blocked", "error": {"code": "unexpected_error", "message": "Publication failed; diagnostic content is suppressed. Review deployment configuration and Entra logs."}}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())