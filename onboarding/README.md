# Engineer Onboarding Guide

Welcome to `mcp-server-polymarket`, an engineered Model Context Protocol (MCP) server and
LangGraph tool suite for the Polymarket APIs. Every design choice in this codebase treats LLMs as
untrusted callers operating near real collateral and private keys: market inputs are treated as
hostile text, signing stays strictly local with fail-closed validation, and every money-moving write
passes through an audited SQLite finite-state journal.

All project workflows standardize on **`uv`** (`uv sync --locked --all-extras`, `uv run ...`,
`uvx ...`) across development, verification, and end-user deployment.

## Curriculum

Read these four chapters in order; each matches executable tests and runtime guards in the
repository:

1. [Domain and Safety Model](01-domain-and-safety.md) — Toolsets, capability gates, fail-closed
   preflight checks, user elicitation, credential isolation, and redaction.
2. [Architecture and Layer DAG](02-architecture-and-layers.md) — The strict 6-layer dependency
   graph, optional `[signing]` lazy boundary (`base-install`), and compact Pydantic boundaries.
3. [State Machine and Verification](03-state-and-verification.md) — The SQLite write-journal FSM,
   property-based tests, strict Ruff and Pyright gates, endpoint coverage, and secret scanning.
4. [Extending and Releasing](04-extending-and-releasing.md) — Adding an operation safely, updating
   API fixtures, running local CI parity checks, and automated tag-driven releases with `uv publish`.

## First 5 Minutes

Verify your local toolchain matches CI before changing anything:

```bash
uv lock --check
uv sync --locked --all-extras
uv run ruff check . && uv run ruff format --check .
uv run pyright
POLYMARKET_TEST_COVERAGE=strict uv run pytest -v
uv run pip-audit
```
