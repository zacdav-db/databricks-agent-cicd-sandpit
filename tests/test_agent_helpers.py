from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    scripts_path = str(ROOT / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


def test_langchain_resolves_the_custom_mcp_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "langchain_custom_mcp",
        ROOT / "src" / "langchain_agent" / "agent.py",
    )
    monkeypatch.setenv("CUSTOM_MCP_APP_NAME", "mcp-dev-sandpit-tools")
    client = SimpleNamespace(
        apps=SimpleNamespace(
            get=lambda *, name: SimpleNamespace(
                url=(
                    "https://mcp-dev-sandpit-tools.example"
                    if name == "mcp-dev-sandpit-tools"
                    else None
                ),
            ),
        ),
    )

    assert module._custom_mcp_url(client) == (
        "https://mcp-dev-sandpit-tools.example/mcp"
    )


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
    monkeypatch.setattr(module, "configure_tracing", lambda: None)
    monkeypatch.setattr(module.mlflow, "start_span", lambda **_kwargs: FakeSpan())

    assert asyncio.run(module.invoke_agent("question")) == (
        "managed MCP answer",
        "trace-id",
    )


def test_langchain_streams_model_chunks_inside_one_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "langchain_agent_streaming",
        ROOT / "src" / "langchain_agent" / "agent.py",
    )

    class FakeSpan:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set_inputs(self, _inputs: dict[str, str]) -> None:
            return None

        def set_outputs(self, outputs: dict[str, str]) -> None:
            assert outputs == {"output": "hello world"}

    class FakeAgent:
        async def astream(
            self,
            _inputs: dict[str, object],
            *,
            stream_mode: str,
        ):
            assert stream_mode == "messages"
            for text in ("hello", " ", "world"):
                yield SimpleNamespace(content=text), {}

    async def create_agent() -> FakeAgent:
        return FakeAgent()

    monkeypatch.setattr(module, "configure_tracing", lambda: None)
    monkeypatch.setattr(module, "_create_tool_agent", create_agent)
    monkeypatch.setattr(module, "_chunk_text", lambda message: message.content)
    monkeypatch.setattr(module.mlflow, "start_span", lambda **_kwargs: FakeSpan())

    async def collect() -> list[str]:
        return [chunk async for chunk in module.stream_agent("question")]

    assert asyncio.run(collect()) == ["hello", " ", "world"]


def test_trace_smoke_requires_a_child_of_the_platform_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "smoke_test_child_span",
        ROOT / "scripts" / "smoke_test.py",
    )
    statements: list[str] = []

    def execute(_client: object, _warehouse: str, statement: str) -> dict[str, object]:
        statements.append(statement)
        return {"result": {"data_array": [["2", "1"]]}}

    monkeypatch.setattr(module, "_execute", execute)
    counts = module._wait_for_trace(
        object(),
        "warehouse",
        "catalog.schema.spans",
        "trace123",
        root_span_name="generated_agent.openai-assistant",
    )

    assert counts == {"trace_rows": 2, "direct_child_rows": 1}
    assert "parent_span_id" in statements[0]
    assert "generated_agent.openai-assistant" in statements[0]


def test_responses_stream_requires_deltas_done_event_and_trace() -> None:
    module = _load(
        "smoke_test_responses_stream",
        ROOT / "scripts" / "smoke_test.py",
    )
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "message-1",
            "delta": "hello ",
        },
        {
            "type": "response.output_text.delta",
            "item_id": "message-1",
            "delta": "world",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "message-1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello world"}],
            },
        },
        {"trace_id": "trace-id"},
    ]
    body = "".join(
        f"data: {json.dumps(event)}\n\n"
        for event in events
    ) + "data: [DONE]\n\n"

    class FakeApiClient:
        def do(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "Content-Type": "text/event-stream; charset=utf-8",
                "contents": io.BytesIO(body.encode()),
            }

    client = SimpleNamespace(api_client=FakeApiClient())
    assert module._responses_stream(client, "https://agent.example", "hello") == {
        "delta_count": 2,
        "output": "hello world",
        "trace_id": "trace-id",
    }


def test_playground_agent_contract_requires_prefix_and_responses_metadata() -> None:
    module = _load(
        "smoke_test_playground_contract",
        ROOT / "scripts" / "smoke_test.py",
    )

    class FakeApiClient:
        def do(self, method: str, **kwargs: object) -> dict[str, str]:
            assert method == "GET"
            assert kwargs["url"] == "https://agent.example/agent/info"
            return {"use_case": "agent", "agent_api": "responses"}

    client = SimpleNamespace(api_client=FakeApiClient())
    assert module._assert_playground_agent(
        client,
        "agent-dev-example",
        "https://agent.example/",
    ) == {"use_case": "agent", "agent_api": "responses"}
    with pytest.raises(RuntimeError, match="must start with agent-"):
        module._assert_playground_agent(
            client,
            "dev-agent-example",
            "https://agent.example",
        )


@pytest.mark.parametrize(
    "agent_info",
    [
        {},
        {"use_case": "chat", "agent_api": "responses"},
        {"use_case": "agent", "agent_api": "chat_completions"},
    ],
)
def test_playground_agent_contract_rejects_invalid_metadata(
    agent_info: dict[str, str],
) -> None:
    module = _load(
        "smoke_test_invalid_playground_contract",
        ROOT / "scripts" / "smoke_test.py",
    )
    client = SimpleNamespace(
        api_client=SimpleNamespace(
            do=lambda *_args, **_kwargs: agent_info,
        ),
    )

    with pytest.raises(RuntimeError, match="not a ResponsesAgent App"):
        module._assert_playground_agent(
            client,
            "agent-dev-example",
            "https://agent.example",
        )


def test_agent_service_inventory_names_and_connection() -> None:
    module = _load(
        "register_uc_agent",
        ROOT / "scripts" / "register_uc_agent.py",
    )
    assert module._inventory_names("prod") == (
        "agent-prod-sandpit-langchain",
        "prod_sandpit_langchain_agent",
        "prod_sandpit_langchain_agent_connection",
    )
    assert module._generated_inventory_names("dev", "langchain-assistant") == (
        "agent-dev-langchain-assistant",
        "dev_agent_langchain_assistant",
        "dev_agent_langchain_assistant_connection",
    )
    assert module._omnigent_inventory_names("prod") == (
        "prod-sandpit-omnigent",
        "prod_sandpit_omnigent",
        "prod_sandpit_omnigent_connection",
    )
    omnigent = module.gateway_agent("dev", runtime_agent="omnigent")
    assert omnigent.app_name == "dev-sandpit-omnigent"
    assert omnigent.base_path == "/v1"
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

        def get(self, _name: str) -> object:
            if self.created is None:
                raise FakeNotFound
            return SimpleNamespace(options=self.created["options"])

        def create(self, **kwargs: object) -> None:
            self.created = kwargs

    class FakeApiClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.service_exists = False

        def do(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((method, path, kwargs))
            if method == "GET" and "permissions/AGENT_SERVICE" in path:
                return {
                    "privilege_assignments": [
                        {
                            "principal": "owner@example.com",
                            "privileges": ["EXECUTE", "READ_METADATA"],
                        },
                    ],
                }
            if method == "GET" and not self.service_exists:
                raise FakeNotFound
            if method == "GET":
                return {
                    "name": "agent-services/catalog_name.schema_name.agent_service",
                    "agent_service_type": "AGENT_SERVICE_TYPE_EXTERNAL",
                    "config": {
                        "connection": {
                            "name": (
                                "connections/"
                                "catalog_name.schema_name.agent_connection"
                            ),
                        },
                        "base_path": "/responses",
                    },
                }
            if method == "POST":
                self.service_exists = True
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
        base_path="/responses",
        system_prompt="Be concise.",
    )
    module._grant_metadata(client, service, "owner@example.com")
    registration = module.GatewayAgent(
        app_name="dev-agent",
        service_name="agent_service",
        connection_name="agent_connection",
        base_path="/responses",
        system_prompt="Be concise.",
    )
    verified = module.verify_gateway_registration(
        client,
        catalog="catalog_name",
        schema="schema_name",
        registration=registration,
        principal="owner@example.com",
        app_url="https://agent.example",
    )

    assert connection == "catalog_name.schema_name.agent_connection"
    assert service == "catalog_name.schema_name.agent_service"
    assert verified["gateway_registered"] is True
    assert verified["privileges"] == ["EXECUTE", "READ_METADATA"]
    assert client.connections.created["parent"] == (
        "schemas/catalog_name.schema_name"
    )
    assert client.connections.created["options"]["client_id"] == "client-id"
    assert client.api_client.calls[1][2]["query"]["agent_service_id"] == (
        "agent_service"
    )
    grant = next(
        call
        for call in client.api_client.calls
        if call[0] == "PATCH" and "permissions/AGENT_SERVICE" in call[1]
    )
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


def test_existing_gateway_connection_requires_the_current_app_origin() -> None:
    module = _load(
        "register_uc_agent_connection_origin",
        ROOT / "scripts" / "register_uc_agent.py",
    )
    current = SimpleNamespace(
        options={"host": "https://agent-old.example"},
    )
    client = SimpleNamespace(
        connections=SimpleNamespace(get=lambda _name: current),
        config=SimpleNamespace(
            client_id=None,
            client_secret=None,
            host="https://workspace.example",
        ),
    )

    with pytest.raises(RuntimeError, match="required to update"):
        module._upsert_connection(
            client,
            catalog="catalog",
            schema="schema",
            connection_name="agent_connection",
            app_url="https://agent-new.example",
        )


def test_gateway_registration_fails_without_required_grants() -> None:
    module = _load(
        "register_uc_agent_missing_grants",
        ROOT / "scripts" / "register_uc_agent.py",
    )
    with pytest.raises(RuntimeError, match="READ_METADATA"):
        module._validate_gateway_registration(
            service={
                "name": "agent-services/catalog_name.schema_name.agent_service",
                "agent_service_type": "AGENT_SERVICE_TYPE_EXTERNAL",
                "config": {
                    "connection": {
                        "name": (
                            "connections/catalog_name.schema_name.agent_connection"
                        ),
                    },
                    "base_path": "/responses",
                },
            },
            grants={
                "privilege_assignments": [
                    {
                        "principal": "owner@example.com",
                        "privileges": ["EXECUTE"],
                    },
                ],
            },
            service_full_name="catalog_name.schema_name.agent_service",
            connection_full_name="catalog_name.schema_name.agent_connection",
            base_path="/responses",
            principal="owner@example.com",
        )


def test_deployment_registers_every_agent_app_in_gateway() -> None:
    deploy_agent = (ROOT / "scripts" / "deploy_agent.sh").read_text(encoding="utf-8")
    deploy_runtime = (ROOT / "scripts" / "deploy_runtime_app.sh").read_text(
        encoding="utf-8",
    )

    assert "scripts/register_uc_agent.py" in deploy_agent
    assert 'component}" == "langchain" || "${component}" == "omnigent"' in (
        deploy_runtime
    )
    assert '--runtime-agent "${component}"' in deploy_runtime


def test_bundle_targets_match_bootstrap_namespaces() -> None:
    bootstrap = _load(
        "bootstrap_target_defaults",
        ROOT / "scripts" / "bootstrap_resources.py",
    )
    app_names = _load(
        "app_names_contract",
        ROOT / "scripts" / "app_names.py",
    )
    langchain_bundle = yaml.safe_load(
        (ROOT / "src" / "langchain_agent" / "databricks.yml").read_text(
            encoding="utf-8",
        ),
    )

    for target in ("dev", "prod"):
        variables = langchain_bundle["targets"][target]["variables"]
        defaults = bootstrap._target_defaults(target)
        assert variables["resource_prefix"] == target
        assert variables["schema"] == defaults["schema"]
        assert variables["trace_table_prefix"] == defaults["table_prefix"]
        assert variables["uc_function_name"] == defaults["cost_function_name"]
        assert variables["uc_time_function_name"] == defaults["time_function_name"]

    assert langchain_bundle["resources"]["apps"]["langchain_agent"]["name"] == (
        app_names.langchain_agent_app_template()
    )
    mcp_bundle = yaml.safe_load(
        (ROOT / "src" / "mcp_server" / "databricks.yml").read_text(
            encoding="utf-8",
        ),
    )
    omnigent_bundle = yaml.safe_load(
        (ROOT / "src" / "omnigent_app" / "databricks.yml").read_text(
            encoding="utf-8",
        ),
    )
    mcp_app_name = mcp_bundle["resources"]["apps"]["mcp_server"]["name"]
    assert mcp_app_name == app_names.mcp_app_template()
    assert "resources" not in mcp_bundle["resources"]["apps"]["mcp_server"]
    langchain_resources = {
        resource["name"]: resource
        for resource in langchain_bundle["resources"]["apps"]["langchain_agent"][
            "resources"
        ]
    }
    omnigent_resources = {
        resource["name"]: resource
        for resource in omnigent_bundle["resources"]["apps"]["omnigent"]["resources"]
    }
    for target in ("dev", "prod"):
        resolved_mcp_name = mcp_app_name.replace(
            "${var.resource_prefix}",
            target,
        )
        assert resolved_mcp_name == app_names.mcp_app_name(target)
        assert (
            langchain_bundle["targets"][target]["variables"]["custom_mcp_app_name"]
            == resolved_mcp_name
        )
        assert (
            omnigent_bundle["targets"][target]["variables"][
                "langchain_agent_app_name"
            ]
            == app_names.langchain_agent_app_name(target)
        )
    assert langchain_resources["custom_mcp_app"]["app"] == {
        "name": "${var.custom_mcp_app_name}",
        "permission": "CAN_USE",
    }
    assert omnigent_resources["langchain_agent_app"]["app"] == {
        "name": "${var.langchain_agent_app_name}",
        "permission": "CAN_USE",
    }
    assert omnigent_bundle["resources"]["apps"]["omnigent"]["name"] == (
        "${var.resource_prefix}-sandpit-omnigent"
    )


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
    assert statements[1].strip().startswith("CREATE FUNCTION IF NOT EXISTS")
    assert "`catalog_name`.`dev_agent_cicd`.`dev_estimate_project_cost`" in (
        statements[1]
    )
    assert statements[2].strip().startswith("CREATE FUNCTION IF NOT EXISTS")
    assert (
        "CONVERT_TIMEZONE(CURRENT_TIMEZONE(), 'UTC', CURRENT_TIMESTAMP())"
        in statements[2]
    )
    assert all("OR REPLACE" not in statement for statement in statements)


def test_ci_promotes_dev_to_main_before_production() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert workflow["on"]["push"]["branches"] == ["dev", "main"]
    assert workflow["on"]["pull_request"]["branches"] == ["dev", "main"]
    assert "workflow_dispatch" not in workflow["on"]
    dev_condition = workflow["jobs"]["deploy-dev"]["if"]
    prod_condition = workflow["jobs"]["deploy-prod"]["if"]
    assert "github.ref == 'refs/heads/dev'" in dev_condition
    assert "github.ref == 'refs/heads/main'" in prod_condition
    assert "needs.select-deployments.outputs.deployable == 'true'" in dev_condition
    assert "needs.select-deployments.outputs.deployable == 'true'" in prod_condition
    selector = workflow["jobs"]["select-deployments"]
    assert selector["outputs"]["selection"] == "${{ steps.select.outputs.selection }}"
    assert selector["outputs"]["deployable"] == "${{ steps.select.outputs.deployable }}"
    assert "select-deployments" in workflow["jobs"]["package-deployment"]["needs"]
    assert "select-deployments" in workflow["jobs"]["deploy-dev"]["needs"]
    assert "select-deployments" in workflow["jobs"]["deploy-prod"]["needs"]
    assert "production-promotion" in workflow["jobs"]["deploy-prod"]["needs"]
    promotion_step = workflow["jobs"]["promotion-source"]["steps"][0]
    assert promotion_step["env"]["HEAD_BRANCH"] == "${{ github.head_ref }}"
    assert "HEAD_REPOSITORY" in promotion_step["env"]
    quality_steps = {
        step["name"]: step.get("run")
        for step in workflow["jobs"]["test"]["steps"]
        if "name" in step
    }
    assert quality_steps["Compose folder-defined agents"] == (
        "python scripts/compose_agents.py"
    )
    assert "validate_agent_dependencies.py" in quality_steps[
        "Resolve App dependencies for Linux and Python 3.11"
    ]
    assert quality_steps["Compile generated Apps with Python 3.11"] == (
        "python -m compileall -q .generated/bundles"
    )
    for job_name in ("deploy-dev", "deploy-prod"):
        deploy_step = workflow["jobs"][job_name]["steps"][-1]
        assert deploy_step["env"]["BASE_SHA"] == "${{ github.event.before }}"
        assert deploy_step["env"]["HEAD_SHA"] == "${{ github.sha }}"
    assert "Block implicit agent deletion or rename" in quality_steps


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


def test_omnigent_launcher_renders_direct_langchain_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "omnigent_launcher",
        ROOT / "src" / "omnigent_app" / "launch.py",
    )
    values = {
        "DATABRICKS_CONFIG_PROFILE": "app",
        "MODEL_ENDPOINT": "model-endpoint",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    bundle = module._render_agent_bundle()
    try:
        config = (bundle / "sandpit_supervisor.yaml").read_text(encoding="utf-8")
        assert "model-endpoint" in config
        assert "type: agent" in config
        assert "agent_tools.invoke_langchain_agent" in config
        assert "type: mcp" not in config
        assert "${" not in config
    finally:
        shutil.rmtree(bundle.parent)


def test_omnigent_launcher_uses_one_identity_behind_app_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "omnigent_launcher_identity",
        ROOT / "src" / "omnigent_app" / "launch.py",
    )
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    monkeypatch.delenv("OMNIGENT_AUTH_HEADER", raising=False)

    module._configure_single_user_identity()

    assert module.os.environ["OMNIGENT_LOCAL_SINGLE_USER"] == "1"
    assert module.os.environ["OMNIGENT_AUTH_HEADER"] == module.LOCAL_AUTH_HEADER
    assert module.LOCAL_AUTH_HEADER != "X-Forwarded-Email"


def test_omnigent_launcher_passes_app_url_to_child_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "omnigent_launcher_runner_env",
        ROOT / "src" / "omnigent_app" / "launch.py",
    )
    monkeypatch.setenv(
        module.RUNNER_ENV_PASSTHROUGH,
        "EXISTING_SETTING, LANGCHAIN_AGENT_URL",
    )

    module._configure_runner_environment()

    assert module.os.environ[module.RUNNER_ENV_PASSTHROUGH] == (
        "EXISTING_SETTING,LANGCHAIN_AGENT_URL"
    )


def test_omnigent_direct_tool_invokes_langchain_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "omnigent_agent_tools",
        ROOT / "src" / "omnigent_app" / "agent_tools.py",
    )
    calls: list[tuple[str, str, dict[str, str]]] = []

    class FakeApiClient:
        def do(
            self,
            method: str,
            *,
            url: str,
            body: dict[str, str],
        ) -> dict[str, str]:
            calls.append((method, url, body))
            return {
                "output": "answer",
                "trace_id": "trace-id",
                "internal": "not part of the tool contract",
            }

    class FakeClient:
        api_client = FakeApiClient()

    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "app")
    monkeypatch.setenv("LANGCHAIN_AGENT_URL", "https://langchain.example/")
    monkeypatch.setattr(module, "_workspace_client", lambda: FakeClient())

    assert module.invoke_langchain_agent("question") == {
        "output": "answer",
        "trace_id": "trace-id",
    }
    assert calls == [
        (
            "POST",
            "https://langchain.example/api/invocations",
            {"input": "question"},
        ),
    ]


def test_custom_mcp_does_not_proxy_to_langchain() -> None:
    server = (ROOT / "src" / "mcp_server" / "server.py").read_text(
        encoding="utf-8",
    )
    assert "invoke_langchain_agent" not in server
    assert "LANGCHAIN_AGENT_APP_NAME" not in server


def test_runtime_change_smokes_consumers_without_redeploying_them() -> None:
    deploy_target = (ROOT / "scripts" / "deploy_target.sh").read_text(
        encoding="utf-8",
    )

    assert "--app topology" in deploy_target
    assert '.apps | index("omnigent") != null' in deploy_target
