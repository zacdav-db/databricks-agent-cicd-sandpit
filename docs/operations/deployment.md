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
3. Creates or updates the target schema, functions, experiment, and trace
   tables.
4. Validates and deploys each selected DAB.
5. Starts, registers, and smoke-tests only each selected unit.

Local deployment selects every unit. CI instead supplies the push's base and
head commits, so an agent-only change deploys only that agent.

## Useful commands

```bash
scripts/deploy_agent.sh dev minimal-assistant <experiment-id>

cd .generated/bundles/minimal-assistant
databricks bundle validate -t dev --var experiment_id=<id>
databricks bundle deploy -t dev --var experiment_id=<id>

cd ../../../src/mcp_server
databricks bundle validate -t dev
databricks bundle deploy -t dev

databricks apps logs dev-sandpit-langchain-agent -p sandpit
databricks apps logs dev-sandpit-mcp-tools -p sandpit
databricks apps logs dev-sandpit-omnigent -p sandpit
databricks apps logs dev-agent-minimal-assistant -p sandpit
```

## Target namespaces

| Target | Schema | Trace prefix |
| --- | --- | --- |
| dev | `zacdav_sandpit_catalog.dev_agent_cicd` | `dev_sandpit_agent_cicd_otel_*` |
| prod | `zacdav_sandpit_catalog.prod_agent_cicd` | `prod_sandpit_agent_cicd_otel_*` |

Both targets intentionally use the same workspace for this sandpit. Their
target-specific names and schemas prevent cross-environment resource binding.
