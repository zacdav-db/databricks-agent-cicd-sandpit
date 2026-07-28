# Local development and deployment

## Prerequisites

- Python 3.12+
- `uv` 0.11.16+
- Databricks CLI 1.7.x
- `jq`
- A valid `sandpit` profile
- OAuth M2M credentials in `DATABRICKS_CLIENT_ID` and
  `DATABRICKS_CLIENT_SECRET` when an Agent Service connection is first created

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  -r requirements-ci.txt \
  -r src/langchain_agent/requirements.txt \
  -r src/mcp_server/requirements.txt
```

Run the same agent composition checks used by CI:

```bash
.venv/bin/python scripts/compose_agents.py
.venv/bin/python scripts/validate_agent_dependencies.py
.venv/bin/ruff check .
.venv/bin/pytest
```

`.generated/` is an ignored build directory. It contains one self-contained
DAB and App source tree per folder, the deployment index, and a one-time legacy
state description used only to transfer existing Apps safely.

## Deploy a target

```bash
bash scripts/deploy_local.sh dev
```

Set `DATABRICKS_CONFIG_PROFILE` to override the default `sandpit` profile.

The deployment is idempotent. It:

1. Composes folder-defined agents.
2. Selects changed deployment units.
3. Creates missing target schema, functions, experiment, and trace tables.
   Existing functions are preserved so an isolated App deployment cannot
   discard the App identity grants attached to them.
4. Validates and deploys each selected DAB.
5. Starts each selected unit.
6. Registers every selected agent App in Unity AI Gateway and verifies its
   Agent Service, connection, base path, and required grants.
7. Smoke-tests each selected agent through the streaming Responses API,
   verifies its trace and provider child span, and checks other selected
   units. A runtime-App change also runs the end-to-end
   Omnigent → LangChain → custom MCP acceptance path without redeploying
   unchanged consumers.
8. After a prefixed MCP replacement passes its smoke test, retires the exact
   legacy non-prefixed MCP App if it exists.

When all runtime Apps are selected, they deploy in dependency order:
`mcp → langchain → omnigent`.

Local deployment selects every unit. CI instead supplies the push's base and
head commits, so an agent-only change deploys only that agent.

Bootstrap deliberately uses `CREATE FUNCTION IF NOT EXISTS`. Changing a
function body is an explicit migration: update it intentionally, then redeploy
each App that needs an `EXECUTE` grant. This keeps unrelated App deployments
from replacing a function and silently dropping another App's permissions.

## Useful commands

```bash
scripts/deploy_agent.sh dev langchain-assistant <experiment-id>

cd .generated/bundles/langchain-assistant
databricks bundle validate -t dev --var experiment_id=<id>
databricks bundle deploy -t dev --var experiment_id=<id>

cd ../../../src/mcp_server
databricks bundle validate -t dev
databricks bundle deploy -t dev

databricks apps logs dev-sandpit-langchain-agent -p sandpit
databricks apps logs mcp-dev-sandpit-tools -p sandpit
databricks apps logs dev-sandpit-omnigent -p sandpit
databricks apps logs dev-agent-langchain-assistant -p sandpit
```

Query a deployed agent with the Databricks OpenAI client:

```python
from databricks_openai import DatabricksOpenAI

client = DatabricksOpenAI()
events = client.responses.create(
    model="apps/dev-agent-langchain-assistant",
    input="Explain streaming in one sentence.",
    stream=True,
)
for event in events:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
```

See the official
[query an agent guide](https://docs.databricks.com/aws/en/agents/agent-framework/query-agent)
for REST, OpenAI client, and MLflow invocation options.

## Target namespaces

| Target | Schema | Trace prefix |
| --- | --- | --- |
| dev | `zacdav_sandpit_catalog.dev_agent_cicd` | `dev_sandpit_agent_cicd_otel_*` |
| prod | `zacdav_sandpit_catalog.prod_agent_cicd` | `prod_sandpit_agent_cicd_otel_*` |

Both targets intentionally use the same workspace for this sandpit. Their
target-specific names and schemas prevent cross-environment resource binding.
