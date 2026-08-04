#!/usr/bin/env python3
"""Store evaluation results in Cosmos DB for historical tracking.

Reads evaluation summary JSON files and stores them in a Cosmos DB
container for long-term tracking and comparison.

Usage:
    python -m evals.store_results \
        --input evals/eval_results/eval_summary_*.json \
        --agent-id mcp-agents \
        --version policy-v1
"""

import argparse
import glob
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions
from azure.identity import DefaultAzureCredential


COSMOS_ENDPOINT = os.getenv("COSMOSDB_ENDPOINT", os.getenv("COSMOS_ACCOUNT_URI", ""))
COSMOS_DATABASE = os.getenv("COSMOSDB_DATABASE_NAME", "mcpdb")
COSMOS_CONTAINER = "evaluation_results"


def get_cosmos_container():
    """Connect to Cosmos DB and return the evaluation_results container."""
    if not COSMOS_ENDPOINT:
        raise RuntimeError(
            "COSMOSDB_ENDPOINT or COSMOS_ACCOUNT_URI environment variable required"
        )

    credential = DefaultAzureCredential()
    client = CosmosClient(COSMOS_ENDPOINT, credential=credential)
    database = client.get_database_client(COSMOS_DATABASE)

    # Create container if it doesn't exist
    try:
        container = database.create_container_if_not_exists(
            id=COSMOS_CONTAINER,
            partition_key=PartitionKey(path="/agent_id"),
        )
    except cosmos_exceptions.CosmosHttpResponseError as e:
        # If create fails (e.g., serverless account), try getting existing
        container = database.get_container_client(COSMOS_CONTAINER)

    return container


def store_eval_summary(container, summary: dict, agent_id: str, version: str):
    """Store a single evaluation summary document."""
    doc_id = str(uuid.uuid4())
    timestamp = summary.get("timestamp", datetime.now(timezone.utc).isoformat())

    document = {
        "id": doc_id,
        "agent_id": agent_id,
        "version": version,
        "type": "evaluation_summary",
        "timestamp": timestamp,
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": summary.get("thresholds", {}),
        "summary": summary.get("summary", {}),
        "all_passed": summary.get("all_passed"),
    }

    container.upsert_item(document)
    return doc_id


def main():
    parser = argparse.ArgumentParser(description="Store evaluation results in Cosmos DB")
    parser.add_argument(
        "--input",
        required=True,
        help="Glob pattern for evaluation summary JSON files",
    )
    parser.add_argument(
        "--agent-id",
        default="mcp-agents",
        help="Agent identifier (default: mcp-agents)",
    )
    parser.add_argument(
        "--version",
        default="v1.0",
        help="Model version label (default: v1.0)",
    )
    args = parser.parse_args()

    # Resolve glob pattern
    files = sorted(glob.glob(args.input))
    if not files:
        print(f"No files matched pattern: {args.input}")
        return 1

    print(f"Found {len(files)} evaluation summary file(s)")
    print(f"Agent ID: {args.agent_id}")
    print(f"Version:  {args.version}")
    print(f"Cosmos:   {COSMOS_ENDPOINT}")
    print(f"Database: {COSMOS_DATABASE}")
    print(f"Container: {COSMOS_CONTAINER}")
    print("-" * 60)

    container = get_cosmos_container()

    for filepath in files:
        filename = os.path.basename(filepath)
        with open(filepath) as f:
            summary = json.load(f)

        doc_id = store_eval_summary(container, summary, args.agent_id, args.version)
        scores = summary.get("summary", {})
        print(
            f"  Stored: {filename} -> {doc_id}"
            f"  (Intent: {scores.get('avg_intent_resolution', 'N/A')}"
            f", Tool: {scores.get('avg_tool_call_accuracy', 'N/A')})"
        )

    print("-" * 60)
    print(f"Successfully stored {len(files)} evaluation result(s) in Cosmos DB")

    # Verify by querying back
    print("\nVerification - recent evaluation records:")
    query = (
        "SELECT c.id, c.agent_id, c.version, c.timestamp, c.summary "
        "FROM c WHERE c.agent_id = @agent_id AND c.type = 'evaluation_summary' "
        "ORDER BY c.timestamp DESC OFFSET 0 LIMIT 5"
    )
    items = list(
        container.query_items(
            query=query,
            parameters=[{"name": "@agent_id", "value": args.agent_id}],
            enable_cross_partition_query=False,
        )
    )
    for item in items:
        s = item.get("summary", {})
        print(
            f"  [{item.get('timestamp', '?')}] v={item.get('version')} "
            f"Intent={s.get('avg_intent_resolution', '?')} "
            f"Tool={s.get('avg_tool_call_accuracy', '?')} "
            f"Passed={s.get('all_passed', '?')}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
