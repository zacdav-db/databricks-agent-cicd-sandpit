"""Canonical target-specific names for deployed agent resources."""

from __future__ import annotations

import argparse

TARGETS = frozenset({"dev", "prod"})
RESOURCE_PREFIX_TEMPLATE = "${var.resource_prefix}"


def _target(value: str) -> str:
    if value not in TARGETS:
        raise ValueError("Target must be dev or prod.")
    return value


def generated_agent_app_name(target: str, agent_name: str) -> str:
    return f"agent-{_target(target)}-{agent_name}"


def generated_agent_app_template(agent_name: str) -> str:
    return f"agent-{RESOURCE_PREFIX_TEMPLATE}-{agent_name}"


def generated_agent_model_name(target: str, agent_name: str) -> str:
    agent_slug = agent_name.replace("-", "_")
    return f"{_target(target)}_agent_{agent_slug}_model"


def generated_agent_model_template(agent_name: str) -> str:
    agent_slug = agent_name.replace("-", "_")
    return f"{RESOURCE_PREFIX_TEMPLATE}_agent_{agent_slug}_model"


def langchain_agent_app_name(target: str) -> str:
    return f"agent-{_target(target)}-sandpit-langchain"


def langchain_agent_app_template() -> str:
    return f"agent-{RESOURCE_PREFIX_TEMPLATE}-sandpit-langchain"


def langchain_agent_model_name(target: str) -> str:
    return f"{_target(target)}_sandpit_langchain_agent_model"


def langchain_agent_model_template() -> str:
    return f"{RESOURCE_PREFIX_TEMPLATE}_sandpit_langchain_agent_model"


def mcp_app_name(target: str) -> str:
    return f"mcp-{_target(target)}-sandpit-tools"


def mcp_app_template() -> str:
    return f"mcp-{RESOURCE_PREFIX_TEMPLATE}-sandpit-tools"


def omnigent_app_name(target: str) -> str:
    return f"{_target(target)}-sandpit-omnigent"


def omnigent_app_template() -> str:
    return f"{RESOURCE_PREFIX_TEMPLATE}-sandpit-omnigent"


def legacy_generated_agent_app_name(target: str, agent_name: str) -> str:
    return f"{_target(target)}-agent-{agent_name}"


def legacy_langchain_agent_app_name(target: str) -> str:
    return f"{_target(target)}-sandpit-langchain-agent"


def legacy_mcp_app_name(target: str) -> str:
    return f"{_target(target)}-sandpit-mcp-tools"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "kind",
        choices=(
            "generated",
            "generated-model",
            "langchain",
            "langchain-model",
            "legacy-generated",
            "legacy-langchain",
            "legacy-mcp",
            "mcp",
            "omnigent",
        ),
    )
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("agent_name", nargs="?")
    args = parser.parse_args()

    if args.kind in {
        "generated",
        "generated-model",
        "legacy-generated",
    } and not args.agent_name:
        parser.error(f"{args.kind} requires agent_name")
    if args.kind == "generated":
        value = generated_agent_app_name(args.target, args.agent_name)
    elif args.kind == "generated-model":
        value = generated_agent_model_name(args.target, args.agent_name)
    elif args.kind == "langchain":
        value = langchain_agent_app_name(args.target)
    elif args.kind == "langchain-model":
        value = langchain_agent_model_name(args.target)
    elif args.kind == "legacy-generated":
        value = legacy_generated_agent_app_name(args.target, args.agent_name)
    elif args.kind == "legacy-langchain":
        value = legacy_langchain_agent_app_name(args.target)
    elif args.kind == "legacy-mcp":
        value = legacy_mcp_app_name(args.target)
    elif args.kind == "omnigent":
        value = omnigent_app_name(args.target)
    else:
        value = mcp_app_name(args.target)
    print(value)


if __name__ == "__main__":
    main()
