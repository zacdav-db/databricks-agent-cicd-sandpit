"""Create resources that must exist before the Asset Bundle app bindings."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.entities.trace_location import UnityCatalog

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f"`{value}`"


def _execute(client: WorkspaceClient, warehouse_id: str, statement: str) -> dict[str, Any]:
    response = client.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=statement,
        wait_timeout="50s",
    )
    payload = response.as_dict()
    state = payload.get("status", {}).get("state")
    if state != "SUCCEEDED":
        error = payload.get("status", {}).get("error", {})
        raise RuntimeError(f"SQL statement failed with state {state}: {error}")
    return payload


def create_uc_function(
    client: WorkspaceClient,
    warehouse_id: str,
    catalog: str,
    schema: str,
    function_name: str,
) -> str:
    full_name = ".".join(
        (_identifier(catalog), _identifier(schema), _identifier(function_name)),
    )
    statement = f"""
    CREATE OR REPLACE FUNCTION {full_name}(
      hours DOUBLE,
      hourly_rate DOUBLE,
      contingency_percent DOUBLE
    )
    RETURNS DOUBLE
    LANGUAGE SQL
    COMMENT 'Estimate project cost including a contingency percentage.'
    RETURN ROUND(hours * hourly_rate * (1.0 + contingency_percent / 100.0), 2)
    """
    _execute(client, warehouse_id, statement)
    _execute(client, warehouse_id, f"SELECT {full_name}(10, 100, 10) AS estimate")
    return f"{catalog}.{schema}.{function_name}"


def create_trace_experiment(
    profile: str | None,
    warehouse_id: str,
    experiment_name: str,
    catalog: str,
    schema: str,
    table_prefix: str,
) -> tuple[str, list[str]]:
    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
        mlflow.set_tracking_uri(f"databricks://{profile}")
    else:
        mlflow.set_tracking_uri("databricks")
    os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = warehouse_id

    experiment = mlflow.set_experiment(
        experiment_name=experiment_name,
        trace_location=UnityCatalog(
            catalog_name=catalog,
            schema_name=schema,
            table_prefix=table_prefix,
        ),
    )
    tables = [
        f"{catalog}.{schema}.{table_prefix}_otel_{suffix}"
        for suffix in ("annotations", "logs", "metrics", "spans")
    ]
    return experiment.experiment_id, tables


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--catalog", default="zacdav_sandpit_catalog")
    parser.add_argument("--schema", default="default")
    parser.add_argument("--warehouse-id", default="f7a871ffa2a9ab80")
    parser.add_argument("--function-name", default="estimate_project_cost")
    parser.add_argument("--experiment-name", default="/Shared/sandpit-agent-cicd-traces")
    parser.add_argument("--table-prefix", default="sandpit_agent_cicd")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    function_name = create_uc_function(
        client,
        args.warehouse_id,
        args.catalog,
        args.schema,
        args.function_name,
    )
    experiment_id, trace_tables = create_trace_experiment(
        args.profile,
        args.warehouse_id,
        args.experiment_name,
        args.catalog,
        args.schema,
        args.table_prefix,
    )
    result = {
        "experiment_id": experiment_id,
        "experiment_name": args.experiment_name,
        "trace_tables": trace_tables,
        "uc_function": function_name,
    }
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"experiment_id={experiment_id}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
