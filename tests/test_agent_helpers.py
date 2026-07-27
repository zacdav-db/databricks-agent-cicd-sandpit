from __future__ import annotations

import asyncio
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import BaseModel

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

    class FakeToolInput(BaseModel):
        value: str

    class FakeManagedTool:
        name = "managed_tool"
        description = "Return a rich MCP result."
        args_schema = FakeToolInput

        async def ainvoke(self, arguments: dict[str, str]) -> list[dict[str, object]]:
            return [
                {
                    "type": "text",
                    "text": {
                        "id": "adapter-field-not-accepted-by-claude",
                        "value": arguments["value"],
                    },
                },
            ]

    class FakeMcpClient:
        async def get_tools(self) -> list[object]:
            return [FakeManagedTool()]

        async def __aenter__(self):
            raise AssertionError("Multi-server MCP clients are not context managers.")

    class FakeAgent:
        def __init__(self, tools: list[object]) -> None:
            self.tools = tools

        async def ainvoke(self, _inputs: dict[str, object]) -> dict[str, object]:
            tool_result = await self.tools[0].ainvoke({"value": "plain"})
            assert isinstance(tool_result, str)
            assert '"value":"plain"' in tool_result
            return {"messages": [SimpleNamespace(content="managed MCP answer")]}

    monkeypatch.setattr(module, "get_model", lambda: object())
    monkeypatch.setattr(module, "WorkspaceClient", lambda: object())
    monkeypatch.setattr(module, "_mcp_client", lambda _client: FakeMcpClient())
    monkeypatch.setattr(
        module,
        "create_agent",
        lambda **kwargs: FakeAgent(kwargs["tools"]),
    )
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
        "prod-sandpit-langchain-agent",
        "prod_sandpit_langchain_agent",
        "prod_sandpit_langchain_agent_connection",
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
        def __init__(self) -> None:
            self.created: dict[str, object] | None = None

        def get(self, _name: str) -> None:
            raise FakeNotFound

        def create(self, **kwargs: object) -> None:
            self.created = kwargs

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
    module._grant_metadata(client, service, "owner@example.com")

    assert connection == "catalog_name.schema_name.agent_connection"
    assert service == "catalog_name.schema_name.agent_service"
    assert client.connections.created["parent"] == (
        "schemas/catalog_name.schema_name"
    )
    assert client.connections.created["options"]["client_id"] == "client-id"
    assert client.api_client.calls[1][2]["query"]["agent_service_id"] == (
        "agent_service"
    )
    grant = client.api_client.calls[2]
    assert grant[:2] == (
        "PATCH",
        (
            "/api/2.1/unity-catalog/permissions/AGENT_SERVICE/"
            "catalog_name.schema_name.agent_service"
        ),
    )
    assert grant[2]["body"]["changes"][0] == {
        "principal": "owner@example.com",
        "add": ["EXECUTE", "READ_METADATA"],
    }


def test_bundle_targets_match_bootstrap_namespaces() -> None:
    bootstrap = _load(
        "bootstrap_target_defaults",
        ROOT / "scripts" / "bootstrap_resources.py",
    )
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text(encoding="utf-8"))
    resources = yaml.safe_load(
        (ROOT / "resources" / "apps.yml").read_text(encoding="utf-8"),
    )

    for target in ("dev", "prod"):
        variables = bundle["targets"][target]["variables"]
        defaults = bootstrap._target_defaults(target)
        assert variables["resource_prefix"] == target
        assert variables["schema"] == defaults["schema"]
        assert variables["trace_table_prefix"] == defaults["table_prefix"]
        assert variables["uc_function_name"] == defaults["cost_function_name"]
        assert variables["uc_time_function_name"] == defaults["time_function_name"]

    apps = resources["resources"]["apps"]
    assert apps["langchain_agent"]["name"] == (
        "${var.resource_prefix}-sandpit-langchain-agent"
    )
    assert apps["mcp_server"]["name"] == "${var.resource_prefix}-sandpit-mcp-tools"
    assert apps["omnigent"]["name"] == "${var.resource_prefix}-sandpit-omnigent"


def test_bootstrap_creates_target_schema_before_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "bootstrap_schema_order",
        ROOT / "scripts" / "bootstrap_resources.py",
    )
    statements: list[str] = []
    monkeypatch.setattr(
        module,
        "_execute",
        lambda _client, _warehouse, statement: statements.append(statement) or {},
    )

    module.create_uc_functions(
        object(),
        "warehouse",
        "catalog_name",
        "dev_agent_cicd",
        "dev_estimate_project_cost",
        "dev_current_utc_timestamp",
    )

    assert statements[0].strip().startswith(
        "CREATE SCHEMA IF NOT EXISTS `catalog_name`.`dev_agent_cicd`",
    )
    assert "`catalog_name`.`dev_agent_cicd`.`dev_estimate_project_cost`" in (
        statements[1]
    )


def test_ci_promotes_dev_to_main_before_production() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert workflow["on"]["push"]["branches"] == ["dev", "main"]
    assert workflow["on"]["pull_request"]["branches"] == ["dev", "main"]
    assert "workflow_dispatch" not in workflow["on"]
    assert workflow["jobs"]["deploy-dev"]["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    )
    assert workflow["jobs"]["deploy-prod"]["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert "production-promotion" in workflow["jobs"]["deploy-prod"]["needs"]
    promotion_step = workflow["jobs"]["promotion-source"]["steps"][0]
    assert promotion_step["env"]["HEAD_BRANCH"] == "${{ github.head_ref }}"
    assert "HEAD_REPOSITORY" in promotion_step["env"]


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
