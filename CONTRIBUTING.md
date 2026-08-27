# Contributing

Kitsune accepts changes that preserve the boundary between the in-process SDK, the external Workspace control plane, and each Agent Application.

## Development setup

Use Python 3.12 or newer, uv, Node.js 24 or newer, pnpm 11, and Docker with the Compose plugin.

```bash
uv sync --all-packages --all-groups
pnpm install --frozen-lockfile
make check
```

See [the development guide](docs/development.md) for focused tests, OpenAPI client generation, database migrations, and Compose E2E checks.

## Change boundaries

Before adding a Core API, decide whether the behavior belongs to the SDK, Workspace, a framework integration, a plugin, an ordinary utility library, or the Agent Application. Do not duplicate Pydantic AI, LangChain, MCP, provider SDK, or OpenTelemetry behavior. Workspace must remain optional for SDK users and must not become an external-traffic proxy or a semantic agent router.

Do not add compatibility aliases for obsolete interfaces. Avoid empty future-facing abstractions and directories with unclear ownership. A behavior change must include its executable validation path and updates to current user or operator documentation.

## Commits and pull requests

Use Conventional Commits unless recent history establishes a more specific convention. Keep each commit focused on one intention. Use a short-lived branch and merge it through a pull request using the repository template.

Report exactly which checks ran. Do not present static inspection as runtime validation when execution is practical, and record any external-only verification that remains.
