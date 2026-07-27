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
TARGET_DEFAULTS = {
    target: {
        "schema": f"{target}_agent_cicd",
        "cost_function_name": f"{target}_estimate_project_cost",
        "time_function_name": f"{target}_current_utc_timestamp",
        "experiment_name": f"/Shared/{target}-sandpit-agent-cicd-traces",
        "table_prefix": f"{target}_sandpit_agent_cicd",
    }
    for target in ("dev", "prod")
}


def _target_defaults(target: str) -> dict[str, str]:
    try:
        return TARGET_DEFAULTS[target]
    except KeyError as exc:
        raise ValueError("Target must be dev or prod.") from exc


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


def create_uc_functions(
    client: WorkspaceClient,
    warehouse_id: str,
    catalog: str,
    schema: str,
    cost_function_name: str,
    time_function_name: str,
) -> list[str]:
    schema_full_name = ".".join((_identifier(catalog), _identifier(schema)))
    _execute(
        client,
        warehouse_id,
        (
            f"CREATE SCHEMA IF NOT EXISTS {schema_full_name} "
            f"COMMENT 'Isolated {schema} resources for the agent CI/CD sandpit'"
        ),
    )
    cost_full_name = ".".join(
        (_identifier(catalog), _identifier(schema), _identifier(cost_function_name)),
    )
    cost_statement = f"""
    CREATE OR REPLACE FUNCTION {cost_full_name}(
      hours DOUBLE,
      hourly_rate DOUBLE,
      contingency_percent DOUBLE
    )
    RETURNS DOUBLE
    LANGUAGE SQL
    COMMENT 'Estimate project cost including a contingency percentage.'
    RETURN ROUND(hours * hourly_rate * (1.0 + contingency_percent / 100.0), 2)
    """
    time_full_name = ".".join(
        (_identifier(catalog), _identifier(schema), _identifier(time_function_name)),
    )
    time_statement = f"""
    CREATE OR REPLACE FUNCTION {time_full_name}()
    RETURNS STRING
    LANGUAGE SQL
    COMMENT 'Return the current UTC timestamp in ISO-8601 format.'
    RETURN CONCAT(DATE_FORMAT(CURRENT_TIMESTAMP(), "yyyy-MM-dd'T'HH:mm:ss.SSS"), 'Z')
    """
    _execute(client, warehouse_id, cost_statement)
    _execute(client, warehouse_id, time_statement)
    _execute(client, warehouse_id, f"SELECT {cost_full_name}(10, 100, 10) AS estimate")
    _execute(client, warehouse_id, f"SELECT {time_full_name}() AS current_time")
    return [
        f"{catalog}.{schema}.{cost_function_name}",
        f"{catalog}.{schema}.{time_function_name}",
    ]


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
    parser.add_argument("--target", required=True, choices=tuple(TARGET_DEFAULTS))
    parser.add_argument("--profile")
    parser.add_argument(
        "--catalog",
        default=os.getenv("UC_CATALOG", "zacdav_sandpit_catalog"),
    )
    parser.add_argument("--schema")
    parser.add_argument(
        "--warehouse-id",
        default=os.getenv("DATABRICKS_WAREHOUSE_ID", "f7a871ffa2a9ab80"),
    )
    parser.add_argument(
        "--function-name",
        default=os.getenv("UC_COST_FUNCTION"),
    )
    parser.add_argument(
        "--time-function-name",
        default=os.getenv("UC_TIME_FUNCTION"),
    )
    parser.add_argument("--experiment-name")
    parser.add_argument(
        "--table-prefix",
        default=os.getenv("UC_TRACE_TABLE_PREFIX"),
    )
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    defaults = _target_defaults(args.target)
    schema = args.schema or os.getenv("UC_SCHEMA") or defaults["schema"]
    cost_function_name = args.function_name or defaults["cost_function_name"]
    time_function_name = args.time_function_name or defaults["time_function_name"]
    experiment_name = args.experiment_name or defaults["experiment_name"]
    table_prefix = args.table_prefix or defaults["table_prefix"]
    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    function_names = create_uc_functions(
        client,
        args.warehouse_id,
        args.catalog,
        schema,
        cost_function_name,
        time_function_name,
    )
    experiment_id, trace_tables = create_trace_experiment(
        args.profile,
        args.warehouse_id,
        experiment_name,
        args.catalog,
        schema,
        table_prefix,
    )
    result = {
        "catalog": args.catalog,
        "cost_function_name": cost_function_name,
        "experiment_id": experiment_id,
        "experiment_name": experiment_name,
        "schema": schema,
        "table_prefix": table_prefix,
        "target": args.target,
        "time_function_name": time_function_name,
        "trace_tables": trace_tables,
        "uc_functions": function_names,
    }
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"experiment_id={experiment_id}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
