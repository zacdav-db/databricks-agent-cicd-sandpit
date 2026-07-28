"""Configure tracing before platform-managed agent code is imported."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from functools import lru_cache

import mlflow

logger = logging.getLogger(__name__)

# provider import, MLflow integration
AUTOTRACE_INTEGRATIONS = (
    ("langchain", "mlflow.langchain"),
    ("openai", "mlflow.openai"),
    ("anthropic", "mlflow.anthropic"),
    ("google.genai", "mlflow.gemini"),
)


def _is_installed(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def _select_experiment() -> None:
    experiment_id = os.getenv("MLFLOW_EXPERIMENT_ID")
    experiment_name = os.getenv("MLFLOW_EXPERIMENT_NAME")
    if experiment_id and experiment_name:
        raise RuntimeError(
            "Set only one of MLFLOW_EXPERIMENT_ID or MLFLOW_EXPERIMENT_NAME.",
        )
    if experiment_id:
        mlflow.set_experiment(experiment_id=experiment_id)
        return
    if experiment_name:
        mlflow.set_experiment(experiment_name=experiment_name)
        return
    raise RuntimeError(
        "MLFLOW_EXPERIMENT_ID or MLFLOW_EXPERIMENT_NAME is required.",
    )


@lru_cache(maxsize=1)
def configure_autologging() -> tuple[str, ...]:
    """Patch every installed provider before the author module is imported."""
    enabled: list[str] = []
    for provider_module, integration_module in AUTOTRACE_INTEGRATIONS:
        if not _is_installed(provider_module):
            continue
        try:
            integration = importlib.import_module(integration_module)
            integration.autolog()
        except Exception as exc:
            raise RuntimeError(
                "MLflow automatic tracing failed for installed provider "
                f"{provider_module!r}.",
            ) from exc
        enabled.append(provider_module)

    logger.info(
        "MLflow tracing configured; automatic integrations=%s",
        enabled or "none",
    )
    return tuple(enabled)


@lru_cache(maxsize=1)
def configure_tracing() -> tuple[str, ...]:
    """Configure the trace destination and installed SDK integrations."""
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "databricks"))
    _select_experiment()
    return configure_autologging()
