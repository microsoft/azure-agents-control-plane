#!/usr/bin/env python3
"""Export a Terraform output map to the active azd environment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def serialize_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the input and print variable names without changing azd.",
    )
    args = parser.parse_args()

    try:
        values = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        print(f"Invalid Terraform output JSON: {error}", file=sys.stderr)
        return 2

    if not isinstance(values, dict):
        print("Terraform output must be a JSON object.", file=sys.stderr)
        return 2

    for name in sorted(values):
        if args.dry_run:
            print(name)
            continue

        subprocess.run(
            ["azd", "env", "set", name, serialize_value(values[name])],
            check=True,
        )

    if not args.dry_run:
        print(f"Exported {len(values)} Terraform outputs to the active azd environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())