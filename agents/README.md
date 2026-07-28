# Folder-defined agents

Each direct child of this directory becomes one isolated Databricks App in
both `dev` and `prod`.

An agent author supplies only:

```text
agents/<name>/
├── agent.yaml
├── agent.py
└── requirements.txt  # optional; exact pins only
```

The manifest is deliberately smaller than `app.yaml`:

```yaml
name: my-agent
model: default
entrypoint: agent:invoke
```

The folder name and `name` must match. `model` is an alias from
`agent_platform/policy.yaml`; it is not an arbitrary serving endpoint.
Names use lowercase letters, digits and hyphens and are at most 19 characters
so the environment-prefixed Databricks App name remains valid.

The entrypoint may be synchronous or asynchronous and must implement:

```python
def invoke(message: str) -> str: ...
```

No platform SDK import is required. Call an existing LangChain, OpenAI Agents
SDK, or custom implementation inside this function. The approved model alias
is resolved to the `MODEL_ENDPOINT` environment variable.

The platform owns FastAPI, routes, health checks, authentication, model
binding, MLflow tracing, the trace experiment, permissions, target naming,
and DAB generation. Authors cannot provide raw environment variables,
resource grants, commands, or DAB fragments.

## Examples

All four examples implement the same one-function contract:

| Folder | Implementation |
| --- | --- |
| [`langchain-assistant`](langchain-assistant) | LangChain `ChatDatabricks` |
| [`gemini-assistant`](gemini-assistant) | Native Google Gen AI SDK |
| [`claude-assistant`](claude-assistant) | Native Anthropic SDK |
| [`openai-assistant`](openai-assistant) | Databricks-authenticated OpenAI SDK |

The provider-native examples create a `WorkspaceClient()` and reuse its host
and short-lived authorization. No provider API key is stored in the folder.

Run the same checks as CI:

```bash
python scripts/compose_agents.py
python scripts/validate_agent_dependencies.py
pytest
```

This is a trusted-contributor contract, not a Python sandbox. Every change
must pass pull-request quality checks before it reaches the protected `dev`
branch. CODEOWNERS requests platform review, but independent approval remains
advisory while this sandpit has only one collaborator.
Removing or renaming a folder is a destructive App change and is blocked in
v1.
