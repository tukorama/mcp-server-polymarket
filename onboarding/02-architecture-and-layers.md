# 2. Architecture and Layer DAG

The codebase under `src/mcp_server_polymarket/` is organized as a **strict 6-layer Directed
Acyclic Graph (DAG)** enforced by an AST test (`tests/test_coverage.py::test_strict_architectural_layer_dag`).
A module at Layer `k` may only execute runtime imports targeting modules at Layer `<= k`.

## Layer Map

```text
Layer 5 (Entry & Surface):  tools, server, cli, integrations, __main__
                                   |
Layer 4 (Transport & Docs): prompts, resources, http_app
                                   |
Layer 3 (Tool Operations):  ops/*
                                   |
Layer 2 (Runtime & Auth):   client/*, identity, capabilities, wallet_tx, auth_flow, safety
                                   |
Layer 1 (Core & Signing):   config, journal, models/*, signing/*
                                   |
Layer 0 (Leaf Utilities):   __init__, errors, protocol, redaction, logging_setup, coverage, knowledge
```

Type-only references that would point upward (such as `Call` in `safety.py`) sit inside
`if TYPE_CHECKING:` blocks so runtime dependencies stay strictly acyclic.

## Optional `[signing]` Boundary

- Base installs (`uvx mcp-server-polymarket`, or CI's `base-install` job using
  `uv sync --locked --no-default-groups`) intentionally omit `eth_account`, `eth_abi`, `msgpack`,
  and LangGraph dependencies.
- `signing/*` modules require `[signing]`. Callers in Layers 2 and 3 (`identity.py`, `auth_flow.py`,
  `wallet_tx.py`, and write modules in `ops/*`) import from `signing` lazily only inside write/auth
  code paths, keeping server startup fast and dependency-light for read-only users.
- The CI `base-install` check verifies `create_server()` never loads `eth_account` into
  `sys.modules`.

## Typed Serialization & Defensive Converters

- Network boundaries parse untrusted JSON through helpers in `models/base.py`:
  - `CompactModel` omits `None` fields at serialization time to minimize agent context bloat.
  - `as_dict`, `require_dict`, `as_list`, `dict_items`, `as_decimal_str`, `iso_time`, and
    `json_list` normalize external responses cleanly without propagating `Unknown` types through
    strict Pyright analysis or mutating shared cached dicts.
