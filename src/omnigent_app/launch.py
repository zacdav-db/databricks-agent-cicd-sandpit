"""Start Omnigent with Databricks App authentication and bundled agent YAML."""

from __future__ import annotations

import configparser
import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request

from databricks.sdk import WorkspaceClient


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing.")
    return value


def _databricks_host() -> str:
    host = _require_env("DATABRICKS_HOST").rstrip("/")
    return host if "://" in host else f"https://{host}"


def _write_app_profile() -> pathlib.Path:
    """Create an ephemeral OAuth profile for Omnigent's Databricks auth adapter."""
    config_dir = pathlib.Path(tempfile.mkdtemp(prefix="omnigent-databricks-"))
    config_path = config_dir / "databrickscfg"
    config = configparser.ConfigParser()
    config["app"] = {
        "host": _databricks_host(),
        "client_id": _require_env("DATABRICKS_CLIENT_ID"),
        "client_secret": _require_env("DATABRICKS_CLIENT_SECRET"),
    }
    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)
    config_path.chmod(0o600)
    return config_path


def _app_url(client: WorkspaceClient, env_name: str) -> str:
    app_name = _require_env(env_name)
    app = client.apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"Databricks App {app_name} does not have a URL.")
    return app.url.rstrip("/")


def _render_agent_bundle() -> pathlib.Path:
    """Materialize the YAML template with deployment-specific values."""
    source = pathlib.Path(__file__).parent / "sandpit_supervisor"
    destination = pathlib.Path(tempfile.mkdtemp(prefix="omnigent-agent-")) / source.name
    shutil.copytree(source, destination)

    replacements = {
        "${CUSTOM_MCP_URL}": _require_env("CUSTOM_MCP_URL"),
        "${DATABRICKS_HOST}": _databricks_host(),
        "${DATABRICKS_CONFIG_PROFILE}": _require_env("DATABRICKS_CONFIG_PROFILE"),
        "${DATABRICKS_WAREHOUSE_ID}": _require_env("DATABRICKS_WAREHOUSE_ID"),
        "${MODEL_ENDPOINT}": _require_env("MODEL_ENDPOINT"),
    }
    for config_path in destination.rglob("*.yaml"):
        rendered = config_path.read_text(encoding="utf-8")
        for template, value in replacements.items():
            rendered = rendered.replace(template, value)
        if "${" in rendered:
            raise RuntimeError(f"Unresolved template variable in {config_path}.")
        config_path.write_text(rendered, encoding="utf-8")
    return destination


def _wait_for_server(url: str, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"Omnigent server did not become ready at {url}.")


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a child process group, allowing a short graceful shutdown."""
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main() -> None:
    client = WorkspaceClient()
    profile_path = _write_app_profile()
    source_root = str(pathlib.Path(__file__).parent)
    existing_pythonpath = os.getenv("PYTHONPATH")
    os.environ["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else source_root
    )
    # Databricks Apps provides the external authentication boundary. Allow the
    # colocated loopback host process to register without a second login flow.
    os.environ["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    os.environ["OMNIGENT_DATABRICKS_EXTRA_HEADERS"] = json.dumps(
        {"X-Forwarded-Email": _require_env("OMNIGENT_HOST_OWNER")},
    )
    os.environ["DATABRICKS_CONFIG_FILE"] = str(profile_path)
    os.environ["DATABRICKS_CONFIG_PROFILE"] = "app"
    os.environ["LANGCHAIN_AGENT_URL"] = _app_url(client, "LANGCHAIN_AGENT_APP_NAME")
    os.environ["CUSTOM_MCP_URL"] = _app_url(client, "CUSTOM_MCP_APP_NAME")
    agent_bundle = _render_agent_bundle()

    port = os.getenv("DATABRICKS_APP_PORT", "8000")
    # Databricks Apps currently builds Python applications with Python 3.11.
    # Omnigent 0.6 requires Python 3.12, so uv supplies a small isolated runtime
    # without changing the platform-managed interpreter.
    uvx_prefix = [
        "uvx",
        "--python",
        "3.12",
        "--from",
        "omnigent[databricks]==0.6.0",
        "omni",
    ]
    server_command = [
        *uvx_prefix,
        "server",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--database-uri",
        "sqlite:////tmp/omnigent.db",
        "--artifact-location",
        "/tmp/omnigent-artifacts",
        "--agent",
        str(agent_bundle),
        "--no-open",
    ]
    local_server_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(server_command, start_new_session=True)
    host: subprocess.Popen[bytes] | None = None

    def shutdown(_signum: int, _frame: object) -> None:
        _stop_process(host)
        _stop_process(server)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        _wait_for_server(local_server_url)
        host = subprocess.Popen(
            [
                *uvx_prefix,
                "host",
                "--server",
                local_server_url,
                "--non-interactive",
            ],
            start_new_session=True,
        )
        raise SystemExit(server.wait())
    finally:
        _stop_process(host)
        _stop_process(server)


if __name__ == "__main__":
    main()
