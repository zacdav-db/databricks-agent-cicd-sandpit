from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_langchain_managed_function_url() -> None:
    module = _load("langchain_agent", ROOT / "src" / "langchain_agent" / "agent.py")
    assert module._function_mcp_url(
        "https://workspace.example/",
        "catalog_name.schema_name.estimate_project_cost",
    ) == (
        "https://workspace.example/api/2.0/mcp/functions/"
        "catalog_name/schema_name/estimate_project_cost"
    )
    with pytest.raises(ValueError, match="catalog.schema.function"):
        module._function_mcp_url("https://workspace.example", "not.fully_qualified")


def test_langchain_uses_supported_multi_server_client_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "langchain_agent_lifecycle",
        ROOT / "src" / "langchain_agent" / "agent.py",
    )

    class FakeSpan:
        trace_id = "trace-id"

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set_inputs(self, _inputs: dict[str, str]) -> None:
            return None

        def set_outputs(self, _outputs: dict[str, str]) -> None:
            return None

    class FakeMcpClient:
        async def get_tools(self) -> list[object]:
            return []

        async def __aenter__(self):
            raise AssertionError("Multi-server MCP clients are not context managers.")

    class FakeAgent:
        async def ainvoke(self, _inputs: dict[str, object]) -> dict[str, object]:
            return {"messages": [SimpleNamespace(content="managed MCP answer")]}

    monkeypatch.setattr(module, "get_model", lambda: object())
    monkeypatch.setattr(module, "WorkspaceClient", lambda: object())
    monkeypatch.setattr(module, "_mcp_client", lambda _client: FakeMcpClient())
    monkeypatch.setattr(module, "create_agent", lambda **_kwargs: FakeAgent())
    monkeypatch.setattr(module.mlflow, "start_span", lambda **_kwargs: FakeSpan())

    assert asyncio.run(module.invoke_agent("question")) == (
        "managed MCP answer",
        "trace-id",
    )


def test_agent_service_inventory_names_and_connection() -> None:
    module = _load(
        "register_uc_agent",
        ROOT / "scripts" / "register_uc_agent.py",
    )
    assert module._inventory_names("prod") == (
        "sandpit-lc-agent-prod",
        "sandpit_langchain_agent_prod",
        "sandpit_langchain_agent_prod_connection",
    )
    options = module._connection_options(
        "https://agent.example/",
        "https://workspace.example/",
        "client-id",
        "client-secret",
    )
    assert options["host"] == "https://agent.example"
    assert options["token_endpoint"] == "https://workspace.example/oidc/v1/token"
    assert options["oauth_scope"] == "all-apis"


def test_uc_registration_extends_the_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "register_uc_agent_sdk",
        ROOT / "scripts" / "register_uc_agent.py",
    )

    class FakeNotFound(Exception):
        pass

    class FakeConnections:
        def get(self, _name: str) -> None:
            raise FakeNotFound

    class FakeApiClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def do(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((method, path, kwargs))
            if method == "GET":
                raise FakeNotFound
            return {}

    class FakeWorkspaceClient:
        connections = FakeConnections()
        api_client = FakeApiClient()
        config = type(
            "Config",
            (),
            {
                "host": "https://workspace.example",
                "client_id": "client-id",
                "client_secret": "client-secret",
            },
        )()

    monkeypatch.setattr(module, "NotFound", FakeNotFound)
    client = FakeWorkspaceClient()

    connection = module._upsert_connection(
        client,
        catalog="catalog_name",
        schema="schema_name",
        connection_name="agent_connection",
        app_url="https://agent.example",
    )
    service = module._upsert_agent_service(
        client,
        catalog="catalog_name",
        schema="schema_name",
        service_name="agent_service",
        connection_full_name=connection,
        target="dev",
    )

    assert connection == "catalog_name.schema_name.agent_connection"
    assert service == "catalog_name.schema_name.agent_service"
    connection_create = client.api_client.calls[0]
    assert connection_create[:2] == (
        "POST",
        "/api/2.1/unity-catalog/connections",
    )
    assert connection_create[2]["body"]["parent"] == (
        "schemas/catalog_name.schema_name"
    )
    assert client.api_client.calls[2][2]["query"]["agent_service_id"] == (
        "agent_service"
    )


def test_omnigent_cost_policy_asks_at_each_new_dollar() -> None:
    module = _load(
        "agent_policies",
        ROOT / "src" / "omnigent_app" / "sandpit_supervisor" / "agent_policies.py",
    )
    policy = module.every_dollar_cost_gate()
    event = {
        "type": "request",
        "context": {"usage": {"total_cost_usd": 2.25}},
        "session_state": {"approved_cost_checkpoint_usd": 1.0},
    }
    response = policy(event)
    assert response["result"] == "ASK"
    assert response["state_updates"][0]["value"] == 2.0

    event["session_state"]["approved_cost_checkpoint_usd"] = 2.0
    assert policy(event) == {"result": "ALLOW"}

    with pytest.raises(ValueError, match="positive"):
        module.every_dollar_cost_gate(0)


def test_omnigent_launcher_renders_uc_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "omnigent_launcher",
        ROOT / "src" / "omnigent_app" / "launch.py",
    )
    values = {
        "CUSTOM_MCP_URL": "https://custom-mcp.example",
        "DATABRICKS_CONFIG_PROFILE": "app",
        "DATABRICKS_HOST": "https://workspace.example",
        "DATABRICKS_WAREHOUSE_ID": "warehouse-id",
        "MODEL_ENDPOINT": "model-endpoint",
        "UC_FUNCTION_FULL_NAME": "catalog_name.schema_name.estimate_project_cost",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    module._set_uc_function_variables()
    bundle = module._render_agent_bundle()
    try:
        config = (bundle / "config.yaml").read_text(encoding="utf-8")
        assert "/catalog_name/schema_name/estimate_project_cost" in config
        assert "catalog_name__schema_name__estimate_project_cost" in config
        assert "${" not in config
    finally:
        shutil.rmtree(bundle.parent)
