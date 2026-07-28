"""Focused acceptance test for one folder-defined agent App."""

from __future__ import annotations

import argparse
import json
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from register_uc_agent import (
    DEFAULT_METADATA_PRINCIPAL,
    gateway_agent,
    verify_gateway_registration,
)
from smoke_test import _api_json, _wait_for_app, _wait_for_trace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--target", required=True, choices=("dev", "prod"))
    parser.add_argument("--profile")
    parser.add_argument(
        "--warehouse-id",
        default=os.getenv("DATABRICKS_WAREHOUSE_ID", "f7a871ffa2a9ab80"),
    )
    parser.add_argument("--trace-table")
    parser.add_argument(
        "--metadata-principal",
        default=DEFAULT_METADATA_PRINCIPAL,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog = os.getenv("UC_CATALOG", "zacdav_sandpit_catalog")
    schema = os.getenv("UC_SCHEMA", f"{args.target}_agent_cicd")
    trace_prefix = os.getenv(
        "UC_TRACE_TABLE_PREFIX",
        f"{args.target}_sandpit_agent_cicd",
    )
    trace_table = (
        args.trace_table or f"{catalog}.{schema}.{trace_prefix}_otel_spans"
    )
    client = WorkspaceClient(
        config=Config(profile=args.profile, http_timeout_seconds=180),
    )
    app_name = f"{args.target}-agent-{args.agent}"
    app_url = _wait_for_app(client, app_name)
    gateway = verify_gateway_registration(
        client,
        catalog=catalog,
        schema=schema,
        registration=gateway_agent(args.target, agent=args.agent),
        principal=args.metadata_principal,
    )
    _api_json(client, "GET", f"{app_url}/api/health")
    result = _api_json(
        client,
        "POST",
        f"{app_url}/api/invocations",
        body={"input": "Reply briefly to confirm this agent is healthy."},
    )
    if not result.get("output") or not result.get("trace_id"):
        raise RuntimeError(f"{app_name} returned an invalid result: {result}")
    trace_counts = _wait_for_trace(
        client,
        args.warehouse_id,
        trace_table,
        result["trace_id"],
        root_span_name=f"generated_agent.{args.agent}",
    )
    print(
        json.dumps(
            {
                "app": app_name,
                "gateway_agent_service": gateway["agent_service"],
                "gateway_registration_verified": True,
                "output": result["output"],
                "trace_id": result["trace_id"],
                "trace_span_counts": trace_counts,
                "trace_table": trace_table,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
