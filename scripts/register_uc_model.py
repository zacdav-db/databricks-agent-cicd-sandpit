"""Register and verify one Databricks App-backed agent model in Unity Catalog."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlflow
from app_names import (
    generated_agent_app_name,
    generated_agent_model_name,
    langchain_agent_app_name,
    langchain_agent_model_name,
)
from mlflow import MlflowClient
from mlflow.models import Model
from mlflow.models.resources import DatabricksApp

DEPLOYED_ALIAS = "deployed"
MODEL_ARTIFACT_NAME = "app_agent"
MODEL_PIP_REQUIREMENTS = (
    "cloudpickle>=3,<4",
    "databricks-sdk==0.122.0",
    "mlflow-skinny==3.14.0",
    "numpy>=1.26,<3",
    "pandas>=2,<3",
)


@dataclass(frozen=True, slots=True)
class ModelRegistration:
    """Resolved names and provenance for one deployed agent."""

    app_name: str
    model_name: str
    target: str
    git_sha: str

    def full_model_name(self, catalog: str, schema: str) -> str:
        return f"{catalog}.{schema}.{self.model_name}"


def model_registration(
    target: str,
    *,
    agent: str | None = None,
    git_sha: str,
) -> ModelRegistration:
    """Resolve the App and UC model names for one deployable agent."""
    if target not in {"dev", "prod"}:
        raise ValueError("Target must be dev or prod.")
    if not git_sha.strip():
        raise ValueError("A non-empty git SHA is required.")
    if agent:
        return ModelRegistration(
            app_name=generated_agent_app_name(target, agent),
            model_name=generated_agent_model_name(target, agent),
            target=target,
            git_sha=git_sha,
        )
    return ModelRegistration(
        app_name=langchain_agent_app_name(target),
        model_name=langchain_agent_model_name(target),
        target=target,
        git_sha=git_sha,
    )


def _matching_version(
    client: MlflowClient,
    full_model_name: str,
    registration: ModelRegistration,
) -> Any | None:
    versions = client.search_model_versions(f"name = '{full_model_name}'")
    return next(
        (
            version
            for version in versions
            if (version.tags or {}).get("source_git_commit") == registration.git_sha
            and (version.tags or {}).get("databricks_app") == registration.app_name
        ),
        None,
    )


def _version_tags(registration: ModelRegistration) -> dict[str, str]:
    return {
        "databricks_app": registration.app_name,
        "deployment_runtime": "databricks_apps",
        "deployment_target": registration.target,
        "source_git_commit": registration.git_sha,
    }


def _set_version_tags(
    client: MlflowClient,
    full_model_name: str,
    version: str,
    tags: dict[str, str],
) -> None:
    for key, value in tags.items():
        client.set_model_version_tag(full_model_name, version, key, value)


def _resource_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {value["name"]} if isinstance(value.get("name"), str) else set()
        for item in value.values():
            names.update(_resource_names(item))
        return names
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names.update(_resource_names(item))
        return names
    return set()


def _verify_model_artifact(
    full_model_name: str,
    version: str,
    app_name: str,
) -> None:
    model = Model.load(f"models:/{full_model_name}/{version}")
    signature = model.signature
    if signature is None:
        raise RuntimeError(
            f"{full_model_name} version {version} has no MLflow signature.",
        )
    signature_text = str(signature.to_dict())
    if "input" not in signature_text or "output" not in signature_text:
        raise RuntimeError(
            f"{full_model_name} version {version} is not a ResponsesAgent model.",
        )
    if not model.flavors.get("python_function", {}).get("streamable"):
        raise RuntimeError(
            f"{full_model_name} version {version} is not streamable.",
        )
    if app_name not in _resource_names(model.resources):
        raise RuntimeError(
            f"{full_model_name} version {version} does not declare "
            f"Databricks App {app_name}.",
        )


def _log_model_version(
    registration: ModelRegistration,
    full_model_name: str,
    experiment_id: str,
) -> str:
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from agent_platform.app_proxy_model import DatabricksAppResponsesAgent

    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")
    mlflow.set_experiment(experiment_id=experiment_id)
    with mlflow.start_run(
        run_name=f"register-{registration.model_name}",
        tags=_version_tags(registration),
    ):
        model_info = mlflow.pyfunc.log_model(
            name=MODEL_ARTIFACT_NAME,
            python_model=DatabricksAppResponsesAgent(registration.app_name),
            code_paths=[str(Path(__file__).resolve().parents[1] / "agent_platform")],
            registered_model_name=full_model_name,
            resources=[DatabricksApp(app_name=registration.app_name)],
            pip_requirements=list(MODEL_PIP_REQUIREMENTS),
            metadata={
                "databricks_app": registration.app_name,
                "deployment_runtime": "databricks_apps",
                "source_git_commit": registration.git_sha,
            },
            await_registration_for=300,
        )
    version = model_info.registered_model_version
    if version is None:
        raise RuntimeError(f"MLflow did not return a version for {full_model_name}.")
    return str(version)


def register_model(
    registration: ModelRegistration,
    *,
    catalog: str,
    schema: str,
    experiment_id: str,
) -> dict[str, str]:
    """Idempotently create a model version, assign its alias, and verify it."""
    full_model_name = registration.full_model_name(catalog, schema)
    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")
    client = MlflowClient(
        tracking_uri="databricks",
        registry_uri="databricks-uc",
    )
    version = _matching_version(client, full_model_name, registration)
    if version is None:
        version_number = _log_model_version(
            registration,
            full_model_name,
            experiment_id,
        )
    else:
        version_number = str(version.version)

    tags = _version_tags(registration)
    _set_version_tags(client, full_model_name, version_number, tags)
    client.set_registered_model_alias(
        full_model_name,
        DEPLOYED_ALIAS,
        version_number,
    )

    verified = client.get_model_version_by_alias(full_model_name, DEPLOYED_ALIAS)
    verified_tags = verified.tags or {}
    if str(verified.version) != version_number:
        raise RuntimeError(
            f"{full_model_name}@{DEPLOYED_ALIAS} points to version "
            f"{verified.version}, expected {version_number}.",
        )
    if any(verified_tags.get(key) != value for key, value in tags.items()):
        raise RuntimeError(
            f"{full_model_name} version {version_number} is missing provenance tags.",
        )
    _verify_model_artifact(
        full_model_name,
        version_number,
        registration.app_name,
    )
    return {
        "alias": DEPLOYED_ALIAS,
        "app_name": registration.app_name,
        "model_name": full_model_name,
        "version": version_number,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    git_sha = os.getenv("GITHUB_SHA")
    parser.add_argument("--target", required=True, choices=("dev", "prod"))
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--git-sha", default=git_sha, required=git_sha is None)
    parser.add_argument("--agent")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registration = model_registration(
        args.target,
        agent=args.agent,
        git_sha=args.git_sha,
    )
    result = register_model(
        registration,
        catalog=args.catalog,
        schema=args.schema,
        experiment_id=args.experiment_id,
    )
    print(
        "Registered "
        f"{result['model_name']} version {result['version']} "
        f"as @{result['alias']} for {result['app_name']}.",
        flush=True,
    )


if __name__ == "__main__":
    main()
