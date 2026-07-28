FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.11.14-slim

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

COPY examples/external-agent/platform-requirements.txt /tmp/platform-requirements.txt
COPY examples/external-agent/requirements.txt /tmp/author-requirements.txt
RUN uv pip install --system --no-cache \
    -r /tmp/platform-requirements.txt \
    -r /tmp/author-requirements.txt

COPY agent_platform/runtime.py /app/_agent_runtime.py
COPY agent_platform/_platform_tracing.py /opt/platform-bootstrap/_platform_tracing.py
COPY examples/external-agent/platform_bootstrap/sitecustomize.py \
    /opt/platform-bootstrap/sitecustomize.py
COPY examples/external-agent/author_agent.py /app/author_agent.py

ENV AGENT_ENTRYPOINT=author_agent:invoke \
    AGENT_NAME=external-openai-agent \
    MLFLOW_TRACKING_URI=databricks \
    PYTHONPATH=/opt/platform-bootstrap \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "_agent_runtime:app", "--host", "0.0.0.0", "--port", "8000"]
