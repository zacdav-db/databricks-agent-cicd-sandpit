"""Register and verify one agent App in the Unity AI Gateway control plane."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import ConnectionType

AGENT_SERVICE_TYPE = "AGENT_SERVICE_TYPE_EXTERNAL"
DEFAULT_METADATA_PRINCIPAL = "zachary.davies@databricks.com"
REQUIRED_PRIVILEGES = frozenset({"EXECUTE", "READ_METADATA"})


@dataclass(frozen=True)
class GatewayAgent:
    """The Gateway registration contract for one deployed agent App."""

    app_name: str
    service_name: str
    connection_name: str
    base_path: str
    system_prompt: str


def _inventory_names(target: str) -> tuple[str, str, str]:
    if target not in {"dev", "prod"}:
        raise ValueError("Target must be dev or prod.")
    stem = f"{target}_sandpit_langchain_agent"
    return f"agent-{target}-sandpit-langchain", stem, f"{stem}_connection"


def _omnigent_inventory_names(target: str) -> tuple[str, str, str]:
    if target not in {"dev", "prod"}:
        raise ValueError("Target must be dev or prod.")
    stem = f"{target}_sandpit_omnigent"
    return f"{target}-sandpit-omnigent", stem, f"{stem}_connection"


def _generated_inventory_names(target: str, name: str) -> tuple[str, str, str]:
    if target not in {"dev", "prod"}:
        raise ValueError("Target must be dev or prod.")
    stem = f"{target}_agent_{name.replace('-', '_')}"
    return f"agent-{target}-{name}", stem, f"{stem}_connection"


def gateway_agent(
    target: str,
    *,
    agent: str | None = None,
    runtime_agent: str = "langchain",
) -> GatewayAgent:
    """Build the required Gateway registration for an agent App."""
    if agent:
        app_name, service_name, connection_name = _generated_inventory_names(
            target,
            agent,
        )
        return GatewayAgent(
            app_name=app_name,
            service_name=service_name,
            connection_name=connection_name,
            base_path="/responses",
            system_prompt=(
                f"Invoke the folder-defined {agent} agent using only resources "
                "granted to its Databricks App identity."
            ),
        )
    if runtime_agent == "langchain":
        app_name, service_name, connection_name = _inventory_names(target)
        return GatewayAgent(
            app_name=app_name,
            service_name=service_name,
            connection_name=connection_name,
            base_path="/responses",
            system_prompt=(
                "You are a concise assistant. Use only the resources granted "
                "to your Databricks App identity."
            ),
        )
    if runtime_agent == "omnigent":
        app_name, service_name, connection_name = _omnigent_inventory_names(target)
        return GatewayAgent(
            app_name=app_name,
            service_name=service_name,
            connection_name=connection_name,
            base_path="/v1",
            system_prompt=(
                "Use the policy-controlled sandpit_supervisor agent and require "
                "its configured approvals for subagents and cost checkpoints."
            ),
        )
    raise ValueError("Runtime agent must be langchain or omnigent.")


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
                "DAB-managed OAuth connection to a sandpit Agent App. Used by "
                "the Unity Catalog Agent Service beta."
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
    base_path: str,
    system_prompt: str,
) -> str:
    full_name = f"{catalog}.{schema}.{service_name}"
    path = f"/api/2.1/unity-catalog/agent-services/{quote(full_name, safe='.')}"
    try:
        existing = workspace_client.api_client.do("GET", path)
    except NotFound:
        existing = None
    comment = (
        f"Agent App deployed by DAB target {target} and registered in Unity AI "
        "Gateway as a Unity Catalog Agent Service."
    )
    config = {
        "connection": {"name": f"connections/{connection_full_name}"},
        "base_path": base_path,
        "system_prompt": system_prompt,
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
                "agent_service_type": AGENT_SERVICE_TYPE,
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


def _validate_gateway_registration(
    *,
    service: dict[str, object],
    grants: dict[str, object],
    service_full_name: str,
    connection_full_name: str,
    base_path: str,
    principal: str,
) -> list[str]:
    """Fail closed unless the Gateway service and grants match the contract."""
    actual_name = str(service.get("name", "")).removeprefix("agent-services/")
    if actual_name != service_full_name:
        raise RuntimeError(
            f"Gateway Agent Service name is {actual_name!r}, "
            f"expected {service_full_name!r}.",
        )
    if service.get("agent_service_type") != AGENT_SERVICE_TYPE:
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} has unexpected type "
            f"{service.get('agent_service_type')!r}.",
        )

    config = service.get("config")
    if not isinstance(config, dict):
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} has no configuration.",
        )
    connection = config.get("connection")
    if not isinstance(connection, dict):
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} has no connection.",
        )
    actual_connection = str(connection.get("name", "")).removeprefix("connections/")
    if actual_connection != connection_full_name:
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} references "
            f"{actual_connection!r}, expected {connection_full_name!r}.",
        )
    if config.get("base_path") != base_path:
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} uses base path "
            f"{config.get('base_path')!r}, expected {base_path!r}.",
        )

    assignments = grants.get("privilege_assignments")
    if not isinstance(assignments, list):
        assignments = []
    privileges = {
        str(privilege)
        for assignment in assignments
        if isinstance(assignment, dict) and assignment.get("principal") == principal
        for privilege in assignment.get("privileges", [])
    }
    missing = REQUIRED_PRIVILEGES - privileges
    if missing:
        raise RuntimeError(
            f"Gateway Agent Service {service_full_name} is missing "
            f"{sorted(missing)} for {principal}.",
        )
    return sorted(privileges)


def verify_gateway_registration(
    workspace_client: WorkspaceClient,
    *,
    catalog: str,
    schema: str,
    registration: GatewayAgent,
    principal: str,
) -> dict[str, object]:
    """Read back and verify the Gateway Agent Service and required grants."""
    service_full_name = f"{catalog}.{schema}.{registration.service_name}"
    connection_full_name = f"{catalog}.{schema}.{registration.connection_name}"
    encoded_name = quote(service_full_name, safe=".")
    service = workspace_client.api_client.do(
        "GET",
        f"/api/2.1/unity-catalog/agent-services/{encoded_name}",
    )
    grants = workspace_client.api_client.do(
        "GET",
        f"/api/2.1/unity-catalog/permissions/AGENT_SERVICE/{encoded_name}",
    )
    if not isinstance(service, dict) or not isinstance(grants, dict):
        raise RuntimeError(
            f"Gateway registration read-back failed for {service_full_name}.",
        )
    privileges = _validate_gateway_registration(
        service=service,
        grants=grants,
        service_full_name=service_full_name,
        connection_full_name=connection_full_name,
        base_path=registration.base_path,
        principal=principal,
    )
    return {
        "agent_service": service_full_name,
        "app": registration.app_name,
        "connection": connection_full_name,
        "gateway_registered": True,
        "privileges": privileges,
    }


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
        default=DEFAULT_METADATA_PRINCIPAL,
    )
    parser.add_argument(
        "--agent-index",
        type=Path,
        default=Path(".generated/agent-index.json"),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--agent",
        help="Register one folder-defined agent App.",
    )
    scope.add_argument(
        "--runtime-agent",
        choices=("langchain", "omnigent"),
        help="Register one explicit runtime agent App.",
    )
    parser.add_argument("--profile")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    schema = args.schema or f"{args.target}_agent_cicd"
    workspace_client = (
        WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    )
    if args.agent:
        generated_index = json.loads(args.agent_index.read_text(encoding="utf-8"))
        known_agents = {agent["name"] for agent in generated_index["agents"]}
        if args.agent not in known_agents:
            raise ValueError(f"Unknown folder-defined agent: {args.agent}")
        registration = gateway_agent(args.target, agent=args.agent)
    else:
        registration = gateway_agent(
            args.target,
            runtime_agent=args.runtime_agent or "langchain",
        )
    app = workspace_client.apps.get(name=registration.app_name)
    if not app.url:
        raise RuntimeError(
            f"Databricks App {registration.app_name} does not have a URL.",
        )

    connection_full_name = _upsert_connection(
        workspace_client,
        catalog=args.catalog,
        schema=schema,
        connection_name=registration.connection_name,
        app_url=app.url,
    )
    agent_service_full_name = _upsert_agent_service(
        workspace_client,
        catalog=args.catalog,
        schema=schema,
        service_name=registration.service_name,
        connection_full_name=connection_full_name,
        target=args.target,
        base_path=registration.base_path,
        system_prompt=registration.system_prompt,
    )
    _grant_metadata(
        workspace_client,
        agent_service_full_name,
        args.metadata_principal,
    )
    verified = verify_gateway_registration(
        workspace_client,
        catalog=args.catalog,
        schema=schema,
        registration=registration,
        principal=args.metadata_principal,
    )
    print(
        json.dumps(
            {
                "gateway_agent_services": [verified],
                "gateway_registration_verified": True,
                "runtime_invocation_available": False,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
