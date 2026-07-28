"""Resolve generated agent dependencies for the Databricks Linux runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--uv", default="uv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated = args.root / ".generated"
    index: dict[str, Any] = json.loads(
        (generated / "agent-index.json").read_text(encoding="utf-8"),
    )
    for agent in index["agents"]:
        name = agent["name"]
        requirements = generated / "agents" / name / "requirements.txt"
        subprocess.run(
            [
                args.uv,
                "pip",
                "compile",
                "--python-version",
                "3.11",
                "--python-platform",
                "x86_64-manylinux_2_28",
                "--only-binary",
                ":all:",
                "--no-sources",
                "--no-header",
                "--no-annotate",
                "--quiet",
                str(requirements),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        print(f"Resolved Linux/Python 3.11 dependencies for {name}.")


if __name__ == "__main__":
    main()
