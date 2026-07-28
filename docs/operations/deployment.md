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

`.generated/` is an ignored build directory. It contains generated App source,
the composed DAB resource file, and the deployment index.

## Deploy a target

```bash
bash scripts/deploy_local.sh dev
```

Set `DATABRICKS_CONFIG_PROFILE` to override the default `sandpit` profile.

The deployment is idempotent. It:

1. Composes folder-defined agents.
2. Creates or updates the target schema and two Unity Catalog functions.
3. Creates or updates the target MLflow experiment and trace tables.
4. Validates and deploys the DAB.
5. Starts every App.
6. Reconciles Unity Catalog Agent Services.
7. Runs the full smoke suite.

## Useful commands

```bash
databricks bundle validate -t dev --var experiment_id=<id>
databricks bundle deploy -t dev --var experiment_id=<id>
databricks bundle run register_uc_agent -t dev --var experiment_id=<id>
databricks bundle run smoke_test -t dev --var experiment_id=<id>

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
