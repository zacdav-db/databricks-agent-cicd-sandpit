# Documentation

The root [README](../README.md) is the starting point. These pages hold the
implementation detail behind its three architecture views.

## Architecture

- [Runtime example](architecture/runtime-example.md): the LangChain,
  custom MCP, managed Unity Catalog tools, Omnigent, and tracing example.
- [CI/CD flow](architecture/cicd.md): protected branches, GitHub Actions,
  target isolation, authentication, and the deployment runner.
- [Folder-defined agents](architecture/folder-defined-agents.md): the minimal
  author contract, generated runtime, validation rules, and design boundaries.

## Operations

- [Local development and deployment](operations/deployment.md): prerequisites,
  setup, target deployment, logs, and useful DAB commands.

The concise author instructions also live beside the agents in
[`agents/README.md`](../agents/README.md).
