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
so the `agent-<environment>-<name>` Databricks App name remains valid and
follows the
[AI Playground App convention](https://docs.databricks.com/aws/en/getting-started/gen-ai-llm-agent#step-3-export-your-agent).

The entrypoint may be synchronous or asynchronous and must implement:

```python
def invoke(message: str) -> str: ...
```

Streaming is optional and uses a convention instead of another manifest
field:

```python
def invoke_stream(message: str):
    yield "first chunk"
    yield "second chunk"
```

`invoke_stream` may return a synchronous or asynchronous iterator of strings.
When present, the platform forwards its chunks through the standard
`/responses` Server-Sent Events stream. Without it, streaming callers still
receive a valid one-chunk response generated from `invoke`.

No platform SDK import is required. Call an existing LangChain, OpenAI Agents
SDK, or custom implementation inside this function. The approved model alias
is resolved to the `MODEL_ENDPOINT` environment variable.

The platform owns FastAPI, routes, health checks, authentication, model
binding, MLflow tracing, the trace experiment, permissions, target naming,
DAB generation, a versioned UC registered model, and mandatory Unity AI
Gateway Agent Service registration. Authors cannot provide raw environment
variables, resource grants, commands, or DAB fragments.

Every composed App exposes the MLflow ResponsesAgent `/responses` and
`/agent/info` surfaces. Deployment smoke tests require the ResponsesAgent
metadata and the `agent-` name before the App can be promoted. The App's DAB
also owns its UC registered model; CI creates a commit-tagged MLflow model
version, assigns the `deployed` alias, and verifies the ResponsesAgent
signature and App dependency. This provides a versioned Unity Catalog release
record while inference continues to run on the App, without a Model Serving
copy. The callable identity used by Responses clients and AI Playground is
`apps/<app-name>`; registering the UC model does not create that selector
entry.

## Examples

All four examples implement the required invocation function and the optional
streaming convention:

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
