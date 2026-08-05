"""MLflow ResponsesAgent model that delegates through the supported Apps API."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks_openai import DatabricksOpenAI
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
        self._openai_client: DatabricksOpenAI | None = None

    def _client(self) -> WorkspaceClient:
        if self._workspace_client is None:
            self._workspace_client = WorkspaceClient()
        return self._workspace_client

    def __getstate__(self) -> dict[str, Any]:
        """Exclude live SDK clients from the MLflow model artifact."""
        return {
            "app_name": self.app_name,
            "_workspace_client": None,
            "_openai_client": None,
        }

    def _responses_client(self) -> DatabricksOpenAI:
        if self._openai_client is None:
            self._openai_client = DatabricksOpenAI(workspace_client=self._client())
        return self._openai_client

    @staticmethod
    def _request_options(request: ResponsesAgentRequest) -> dict[str, Any]:
        options = request.model_dump(exclude_none=True)
        options.pop("stream", None)
        extension_fields = {
            name: options.pop(name)
            for name in ("custom_inputs", "context")
            if name in options
        }
        if extension_fields:
            options["extra_body"] = extension_fields
        return options

    def predict(
        self,
        request: ResponsesAgentRequest,
    ) -> ResponsesAgentResponse:
        response = self._responses_client().responses.create(
            model=f"apps/{self.app_name}",
            stream=False,
            **self._request_options(request),
        )
        return ResponsesAgentResponse(**response.model_dump(exclude_none=True))

    def predict_stream(
        self,
        request: ResponsesAgentRequest,
    ) -> Iterator[ResponsesAgentStreamEvent]:
        stream = self._responses_client().responses.create(
            model=f"apps/{self.app_name}",
            stream=True,
            **self._request_options(request),
        )
        for event in stream:
            yield ResponsesAgentStreamEvent(**event.model_dump(exclude_none=True))
