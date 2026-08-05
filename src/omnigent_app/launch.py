"""Start Omnigent with Databricks App authentication and bundled agent YAML."""

from __future__ import annotations

import configparser
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request

LOCAL_AUTH_HEADER = "X-Omnigent-Local-Identity"
RUNNER_ENV_PASSTHROUGH = "OMNIGENT_RUNNER_ENV_PASSTHROUGH"


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


def _configure_single_user_identity() -> None:
    """Use one Omnigent identity behind the Databricks App auth boundary."""
    os.environ["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    os.environ["OMNIGENT_AUTH_HEADER"] = LOCAL_AUTH_HEADER


def _configure_runner_environment() -> None:
    """Pass the deployment-owned App name to Omnigent child runners."""
    names = {
        name.strip()
        for name in os.getenv(RUNNER_ENV_PASSTHROUGH, "").split(",")
        if name.strip()
    }
    names.add("LANGCHAIN_AGENT_APP_NAME")
    os.environ[RUNNER_ENV_PASSTHROUGH] = ",".join(sorted(names))


def _render_agent_bundle() -> pathlib.Path:
    """Materialize the YAML template with deployment-specific values."""
    source = pathlib.Path(__file__).parent / "sandpit_supervisor"
    destination = pathlib.Path(tempfile.mkdtemp(prefix="omnigent-agent-")) / source.name
    shutil.copytree(source, destination)

    replacements = {
        "${DATABRICKS_CONFIG_PROFILE}": _require_env("DATABRICKS_CONFIG_PROFILE"),
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


def _wait_for_server(
    url: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: int = 120,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            raise RuntimeError(f"Omnigent server exited unexpectedly with {status}.")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
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


def _wait_for_children(
    server: subprocess.Popen[bytes],
    host: subprocess.Popen[bytes],
) -> int:
    while True:
        server_status = server.poll()
        if server_status is not None:
            return server_status
        host_status = host.poll()
        if host_status is not None:
            raise RuntimeError(f"Omnigent host exited unexpectedly with {host_status}.")
        time.sleep(1)


def main() -> None:
    profile_path = _write_app_profile()
    agent_bundle: pathlib.Path | None = None
    server: subprocess.Popen[bytes] | None = None
    host: subprocess.Popen[bytes] | None = None

    def shutdown(_signum: int, _frame: object) -> None:
        _stop_process(host)
        _stop_process(server)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        source_root = str(pathlib.Path(__file__).parent)
        existing_pythonpath = os.getenv("PYTHONPATH")
        os.environ["PYTHONPATH"] = (
            f"{source_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else source_root
        )
        # Databricks Apps provides the external authentication boundary. Keep
        # this example as one Omnigent user so callers using user or
        # service-principal OAuth can see the same colocated host.
        _configure_single_user_identity()
        os.environ["DATABRICKS_CONFIG_FILE"] = str(profile_path)
        os.environ["DATABRICKS_CONFIG_PROFILE"] = "app"
        _require_env("LANGCHAIN_AGENT_APP_NAME")
        _configure_runner_environment()
        agent_bundle = _render_agent_bundle()

        port = os.getenv("DATABRICKS_APP_PORT", "8000")
        # Databricks Apps builds Python applications with Python 3.11.
        # Omnigent 0.6 requires 3.12, so uv supplies an isolated runtime.
        uvx_prefix = [
            "uvx",
            "--python",
            "3.12",
            "--from",
            "omnigent[databricks]==0.6.0",
            "omni",
        ]
        server = subprocess.Popen(
            [
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
            ],
            start_new_session=True,
        )
        local_server_url = f"http://127.0.0.1:{port}"
        _wait_for_server(local_server_url, server)
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
        raise SystemExit(_wait_for_children(server, host))
    finally:
        _stop_process(host)
        _stop_process(server)
        shutil.rmtree(profile_path.parent, ignore_errors=True)
        if agent_bundle is not None:
            shutil.rmtree(agent_bundle.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
