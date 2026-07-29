"""MLflow ResponsesAgent model that delegates inference to a Databricks App."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from databricks.sdk import WorkspaceClient
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)


class DatabricksAppResponsesAgent(ResponsesAgent):
    """Expose an agent App as a versioned, standard-signature MLflow model."""

    def __init__(self, app_name: str) -> None:
        if not app_name.startswith("agent-"):
            raise ValueError("Agent App names must start with agent-.")
        self.app_name = app_name
        self._workspace_client: WorkspaceClient | None = None
        self._app_url: str | None = None

    def _client(self) -> WorkspaceClient:
        if self._workspace_client is None:
            self._workspace_client = WorkspaceClient()
        return self._workspace_client

    def __getstate__(self) -> dict[str, Any]:
        """Exclude live SDK clients from the MLflow model artifact."""
        return {
            "app_name": self.app_name,
            "_workspace_client": None,
            "_app_url": None,
        }

    def _url(self) -> str:
        if self._app_url is None:
            app = self._client().apps.get(name=self.app_name)
            if not app.url:
                raise RuntimeError(f"Databricks App {self.app_name} has no URL.")
            self._app_url = f"{app.url.rstrip('/')}/responses"
        return self._app_url

    @staticmethod
    def _payload(request: ResponsesAgentRequest, *, stream: bool) -> dict[str, Any]:
        payload = request.model_dump(exclude_none=True)
        payload["stream"] = stream
        return payload

    def predict(
        self,
        request: ResponsesAgentRequest,
    ) -> ResponsesAgentResponse:
        response = self._client().api_client.do(
            "POST",
            url=self._url(),
            body=self._payload(request, stream=False),
        )
        if not isinstance(response, dict):
            raise RuntimeError("Agent App returned a non-object response.")
        return ResponsesAgentResponse(**response)

    def predict_stream(
        self,
        request: ResponsesAgentRequest,
    ) -> Iterator[ResponsesAgentStreamEvent]:
        response = self._client().api_client.do(
            "POST",
            url=self._url(),
            body=self._payload(request, stream=True),
            headers={"Accept": "text/event-stream"},
            raw=True,
            response_headers=["Content-Type"],
        )
        if (
            not isinstance(response, dict)
            or "text/event-stream" not in response.get("Content-Type", "")
            or "contents" not in response
        ):
            raise RuntimeError("Agent App did not return an SSE response.")

        with response["contents"] as contents:
            for chunk in contents:
                for line in chunk.decode("utf-8").splitlines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        return
                    event = json.loads(data)
                    if error := event.get("error"):
                        raise RuntimeError(f"Agent App stream failed: {error}")
                    yield ResponsesAgentStreamEvent(**event)
