# Runtime example

This example shows how several agent and tool styles can coexist while each
Databricks App retains its own Asset Bundle state, App identity, and governed
resource bindings.

The same topology is deployed twice. `dev` and `prod` share the sandpit
workspace, catalog, model endpoint, and SQL warehouse, but use different App
names, schemas, functions, experiments, Agent Services, connections, bundle
roots, and trace tables.

## One target

```mermaid
flowchart LR
    User["User or API client"]
    Omni["Omnigent App"]
    MCP["Custom MCP App<br/>(standalone tool server)"]
    Agent["LangChain App"]
    Generated["Folder-defined agent App"]
    Managed["Managed Functions MCP"]
    Functions["Unity Catalog functions"]
    Model["Foundation Model API"]
    Gateway["Unity AI Gateway<br/>UC Agent Services"]
    Traces[("Unity Catalog trace tables")]

    User --> Omni
    Omni -->|"direct App invocation"| Agent
    Agent -->|"custom tools"| MCP
    Agent -->|"governed tools"| Managed
    Managed -->|"execute"| Functions
    Omni -->|"supervision"| Model
    Agent -->|"inference"| Model
    Generated -->|"inference"| Model
    Agent -->|"MLflow spans"| Traces
    Generated -->|"MLflow spans"| Traces
    Gateway -. "governed inventory" .-> Agent
    Gateway -. "governed inventory" .-> Generated
    Gateway -. "governed inventory" .-> Omni
```

## Components

| Component | Purpose |
| --- | --- |
| `*-sandpit-langchain-agent` | FastAPI LangChain agent using a Databricks Foundation Model, the custom MCP App, and managed Unity Catalog function MCP servers. |
| `mcp-*-sandpit-tools` | Standalone custom Streamable HTTP MCP server. It exposes tools but has no agent dependency. The `mcp-` prefix makes the App discoverable as an MCP server in AI Playground. |
| `*-sandpit-omnigent` | Omnigent supervisor that delegates directly to the LangChain App and applies approval policies. |
| `*-agent-langchain-assistant` | Example App using LangChain through the folder-defined agent contract. |
| Managed Functions MCP | Databricks-managed MCP surface over the target's Unity Catalog functions. |
| Unity AI Gateway | Governed Agent Service inventory and permissions for every agent App. |
| Trace tables | Four governed OpenTelemetry tables backing the target's MLflow experiment. |

## Unity Catalog mapping

Each DAB maps its supported objects at the strongest level currently available:

- The cost and current-time tools are three-level Unity Catalog `FUNCTION`
  securables. The LangChain App receives `EXECUTE` through DAB
  `uc_securable` bindings and discovers them through managed Functions MCP
  endpoints.
- The LangChain DAB declares the custom MCP App as a
  [Databricks App resource](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/apps-resource)
  with `CAN_USE`. The LangChain App resolves its target-specific URL and loads
  its tools alongside the managed function tools.
- The Omnigent DAB declares the LangChain App as a
  [Databricks App resource](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/apps-resource)
  with `CAN_USE`. Its deployment-owned function tool invokes LangChain's
  `/api/invocations` endpoint directly.
- The fixed LangChain App, Omnigent supervisor, and every folder-defined agent
  are registered after deployment as target-specific Unity Catalog Agent
  Services in Unity AI Gateway. The sandpit owner receives `EXECUTE` and
  `READ_METADATA`.
- Registration is fail-closed. CI reads the service and permission records
  back and checks the expected App connection, base path, service type, and
  grants before smoke testing the agent.
- Agent Services currently provide beta inventory and permissions. Live
  traffic continues to use the DAB-deployed App endpoints.
- The custom MCP remains a stateless Databricks App. Databricks does not
  currently support registering an App as a Unity Catalog MCP Service. Its
  App name starts with `mcp-`, as required for AI Playground discovery.
- Trace data is governed in Unity Catalog rather than written to a shared,
  cross-target table.

DAB CLI `1.7.x` has no first-class resource types for UC functions, HTTP
connections, Agent Services, or MCP Services. Deployment therefore combines
native App resources with two platform scripts:

- `bootstrap_resources.py` creates the target schema, functions, and MLflow
  experiment before App bindings are deployed.
- `register_uc_agent.py` uses `WorkspaceClient` and its authenticated API
  client to reconcile and verify the beta Gateway Agent Service resources.

See the Databricks documentation for
[hosting custom MCP servers as Apps](https://docs.databricks.com/aws/en/agents/mcp/custom-mcp),
[managed MCP servers](https://docs.databricks.com/aws/en/agents/mcp/managed-mcp),
[Agent Services](https://docs.databricks.com/aws/en/ai-gateway/agent-services),
and
[MCP Service limitations](https://docs.databricks.com/aws/en/agents/mcp/mcp-services).

## Trace storage

Each target has an MLflow experiment backed by four SQL-queryable Unity
Catalog OpenTelemetry tables:

| Target | MLflow experiment | UC schema | Table prefix |
| --- | --- | --- | --- |
| dev | `/Shared/dev-sandpit-agent-cicd-traces` | `zacdav_sandpit_catalog.dev_agent_cicd` | `dev_sandpit_agent_cicd_otel_*` |
| prod | `/Shared/prod-sandpit-agent-cicd-traces` | `zacdav_sandpit_catalog.prod_agent_cicd` | `prod_sandpit_agent_cicd_otel_*` |

Each prefix expands to `annotations`, `logs`, `metrics`, and `spans`. Nothing
in either target binds to the other target's schema. Each deployment runs a
focused smoke test for only the selected App; agent tests wait until their
trace is queryable in the target's spans table.

## Omnigent behavior

The supervisor definition is
[`src/omnigent_app/sandpit_supervisor/sandpit_supervisor.yaml`](../../src/omnigent_app/sandpit_supervisor/sandpit_supervisor.yaml).
It demonstrates:

- A subagent that invokes the LangChain App directly with Databricks App
  authentication.
- Indirect access to custom MCP and governed Unity Catalog function tools
  through LangChain's tool-calling loop.
- An `ASK` policy before `sys_session_send` or `sys_session_create`.
- An `ASK` policy whenever cumulative Omnigent spend reaches a new whole
  dollar checkpoint.

Omnigent evaluates spend at turn and tool boundaries. If one turn crosses
several checkpoints, it asks once at the highest newly crossed checkpoint.
The policy measures Omnigent's supervisory LLM spend. The downstream
LangChain App records its own MLflow trace, including its model and MCP tool
spans. The custom MCP is independently deployable and contains no callback or
bridge to LangChain.

The sandpit supervisor is one Omnigent user behind the
[Databricks Apps authentication boundary](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth).
This keeps its colocated host visible to both interactive users and the CI
service principal without bypassing App authentication.

Databricks Apps supplies Python 3.11, while Omnigent 0.6 requires Python 3.12.
The Omnigent launcher uses `uvx` for an isolated Python 3.12 runtime. This
sandpit uses Omnigent local single-user mode; a multi-user rollout should use
its shared-server SSO mode.
