# Externally hosted agent

This example runs outside Databricks while sending its MLflow traces to a
Databricks experiment. The author-owned
[`author_agent.py`](author_agent.py) is an ordinary OpenAI implementation: it
does not import MLflow, Databricks, or a platform SDK.

The deployment adds two platform-owned layers:

- `sitecustomize.py` is loaded by Python before application imports and enables
  MLflow autologging for every supported provider package that is installed.
- `_agent_runtime.py` owns the Responses API, streaming boundary, and root
  span around `author_agent:invoke` or `author_agent:invoke_stream`.

Build from the repository root:

```bash
docker build \
  -f examples/external-agent/Dockerfile \
  -t external-openai-agent .
```

For this sandpit, read back the already-provisioned dev experiment ID:

```bash
export MLFLOW_EXPERIMENT_ID="$(
  databricks experiments get-by-name \
    /Shared/dev-sandpit-agent-cicd-traces \
    -p sandpit \
    -o json |
    jq -er '
      .experiment
      | select(any(
          .tags[]?;
          .key == "mlflow.experiment.databricksTraceDestinationPath"
          and .value == "zacdav_sandpit_catalog.dev_agent_cicd.dev_sandpit_agent_cicd"
        ))
      | .experiment_id
    '
)"
```

Run it with OpenAI credentials and Databricks OAuth M2M credentials:

```bash
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY \
  -e OPENAI_MODEL=gpt-4.1-mini \
  -e DATABRICKS_HOST \
  -e DATABRICKS_AUTH_TYPE=oauth-m2m \
  -e DATABRICKS_CLIENT_ID \
  -e DATABRICKS_CLIENT_SECRET \
  -e MLFLOW_EXPERIMENT_ID \
  external-openai-agent
```

The service principal must be assigned to the workspace and have permission to
write to the experiment and its Unity Catalog trace location. The experiment
ID must identify an existing Unity Catalog-backed experiment; selecting by ID
prevents a misspelled name from creating an ordinary workspace experiment.
The repository's `scripts/bootstrap_resources.py` provisions this for its dev
and prod targets, or follow the official
[Unity Catalog trace storage guide](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/storage).

Publish the container behind the external platform's TLS and authentication
boundary. The example runtime focuses on invocation and tracing. Invoke a
local container with:

```bash
curl --no-buffer http://localhost:8000/responses \
  --header 'Accept: text/event-stream' \
  --header 'Content-Type: application/json' \
  --header 'x-mlflow-return-trace-id: true' \
  --data '{
    "input": [{"role": "user", "content": "What makes a trace useful?"}],
    "stream": true
  }'
```

The Server-Sent Events include output-text deltas, the completed item, the
root MLflow trace ID, and a terminal `[DONE]` event. The OpenAI call appears as
a child span because its SDK was installed before the deployment-owned
bootstrap ran. The image pins the full MLflow Databricks extra because the
platform runtime uses MLflow AgentServer as well as tracing.

## Optional Gateway registration

An external endpoint can also be represented as a Unity Catalog Agent Service.
First create an HTTP connection for its HTTPS origin and authentication. Then
replace the placeholders in [`agent-service.json`](agent-service.json) and
register it:

```bash
databricks api post \
  "/api/2.1/unity-catalog/agent-services\
?parent=schemas/<catalog>.<schema>\
&agent_service_id=external_openai_agent" \
  --json @examples/external-agent/agent-service.json
```

Agent Service registration currently supplies governed inventory and
permissions; it does not proxy runtime invocation. Clients still call the
external endpoint, and the MLflow bootstrap—not Gateway registration—creates
the trace.

See the official Databricks guides for
[external production tracing](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/prod-tracing-external),
[OAuth service-principal authentication](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m),
[HTTP connections](https://docs.databricks.com/aws/en/query-federation/http),
and
[Agent Services](https://docs.databricks.com/aws/en/ai-gateway/agent-services).
