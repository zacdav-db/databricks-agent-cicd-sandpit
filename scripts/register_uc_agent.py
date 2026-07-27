"""Register the deployed LangChain App in the Unity Catalog Agent Service beta."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote, urlsplit

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import ConnectionType


def _inventory_names(target: str) -> tuple[str, str, str]:
    if target not in {"dev", "prod"}:
        raise ValueError("Target must be dev or prod.")
    stem = f"{target}_sandpit_langchain_agent"
    return f"{target}-sandpit-langchain-agent", stem, f"{stem}_connection"


def _connection_options(
    app_url: str,
    workspace_host: str,
    client_id: str,
    client_secret: str,
) -> dict[str, str]:
    parsed = urlsplit(app_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("The Databricks App URL must be an HTTPS origin.")
    return {
        "host": f"https://{parsed.netloc}",
        "port": "443",
        "base_path": "/",
        "client_id": client_id,
        "client_secret": client_secret,
        "oauth_scope": "all-apis",
        "token_endpoint": f"{workspace_host.rstrip('/')}/oidc/v1/token",
        "oauth_credential_exchange_method": "header_and_body",
    }


def _upsert_connection(
    workspace_client: WorkspaceClient,
    *,
    catalog: str,
    schema: str,
    connection_name: str,
    app_url: str,
) -> str:
    full_name = f"{catalog}.{schema}.{connection_name}"
    try:
        workspace_client.connections.get(full_name)
        connection_exists = True
    except NotFound:
        connection_exists = False

    client_id = workspace_client.config.client_id
    client_secret = workspace_client.config.client_secret
    if not client_id or not client_secret:
        if not connection_exists:
            raise RuntimeError(
                "DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET are required "
                "to create the Agent Service connection.",
            )
        return full_name

    options = _connection_options(
        app_url,
        workspace_client.config.host,
        client_id,
        client_secret,
    )
    if not connection_exists:
        workspace_client.connections.create(
            name=connection_name,
            parent=f"schemas/{catalog}.{schema}",
            connection_type=ConnectionType.HTTP,
            comment=(
                "DAB-managed OAuth connection to the sandpit LangChain Agent App. "
                "Used by the Unity Catalog Agent Service beta."
            ),
            options=options,
        )
    else:
        workspace_client.connections.update(name=full_name, options=options)
    return full_name


def _upsert_agent_service(
    workspace_client: WorkspaceClient,
    *,
    catalog: str,
    schema: str,
    service_name: str,
    connection_full_name: str,
    target: str,
) -> str:
    full_name = f"{catalog}.{schema}.{service_name}"
    path = f"/api/2.1/unity-catalog/agent-services/{quote(full_name, safe='.')}"
    try:
        existing = workspace_client.api_client.do("GET", path)
    except NotFound:
        existing = None
    comment = (
        f"LangChain delivery-planning agent deployed by DAB target {target}. "
        "Agent Service is discovery metadata while beta runtime invocation is unavailable."
    )
    config = {
        "connection": {"name": f"connections/{connection_full_name}"},
        "base_path": "/api/invocations",
        "system_prompt": (
            "You are a concise delivery-planning assistant using governed "
            "Unity Catalog function tools."
        ),
    }
    if existing is None:
        workspace_client.api_client.do(
            "POST",
            "/api/2.1/unity-catalog/agent-services",
            query={
                "parent": f"schemas/{catalog}.{schema}",
                "agent_service_id": service_name,
            },
            body={
                "agent_service_type": "AGENT_SERVICE_TYPE_EXTERNAL",
                "comment": comment,
                "config": config,
            },
        )
    else:
        existing_connection = (
            existing.get("config", {}).get("connection", {}).get("name", "")
        )
        if existing_connection.removeprefix("connections/") != connection_full_name:
            raise RuntimeError(
                f"Agent Service {full_name} references unexpected connection "
                f"{existing_connection!r}.",
            )
        workspace_client.api_client.do(
            "PATCH",
            path,
            query={"update_mask": "comment,config.system_prompt,config.base_path"},
            body={"comment": comment, "config": config},
        )
    return full_name


def _grant_metadata(
    workspace_client: WorkspaceClient,
    agent_service_full_name: str,
    principal: str,
) -> None:
    workspace_client.api_client.do(
        "PATCH",
        (
            "/api/2.1/unity-catalog/permissions/AGENT_SERVICE/"
            f"{quote(agent_service_full_name, safe='.')}"
        ),
        body={
            "changes": [
                {
                    "principal": principal,
                    "add": ["EXECUTE", "READ_METADATA"],
                },
            ],
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=("dev", "prod"))
    parser.add_argument(
        "--catalog",
        default=os.getenv("UC_CATALOG", "zacdav_sandpit_catalog"),
    )
    parser.add_argument("--schema", default=os.getenv("UC_SCHEMA"))
    parser.add_argument(
        "--metadata-principal",
        default="zachary.davies@databricks.com",
    )
    parser.add_argument("--profile")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    schema = args.schema or f"{args.target}_agent_cicd"
    app_name, service_name, connection_name = _inventory_names(args.target)
    workspace_client = (
        WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    )
    app = workspace_client.apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"Databricks App {app_name} does not have a URL.")

    connection_full_name = _upsert_connection(
        workspace_client,
        catalog=args.catalog,
        schema=schema,
        connection_name=connection_name,
        app_url=app.url,
    )
    agent_service_full_name = _upsert_agent_service(
        workspace_client,
        catalog=args.catalog,
        schema=schema,
        service_name=service_name,
        connection_full_name=connection_full_name,
        target=args.target,
    )
    _grant_metadata(
        workspace_client,
        agent_service_full_name,
        args.metadata_principal,
    )
    print(
        json.dumps(
            {
                "agent_service": agent_service_full_name,
                "app": app_name,
                "connection": connection_full_name,
                "runtime_invocation_available": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
