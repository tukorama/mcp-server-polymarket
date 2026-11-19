# Contributing

```bash
uv sync --all-extras
uv run pytest
uv run ruff check && uv run ruff format --check && uv run pyright
```

To try the server interactively:

```bash
uv run mcp dev src/mcp_server_polymarket/server.py
```

Adding a tool means adding one decorated function in `src/mcp_server_polymarket/ops/`. The same
function becomes the MCP tool and the LangChain tool, and its docstring is what the model reads, so
say what it returns and when to use a sibling instead. A tool that places orders or moves funds
also needs a `describe` function, which writes the confirmation text, and must return a
`WriteResult`. Every HTTP call goes through an `Endpoint` constant in `client/endpoints.py`.

Tests use scripted responses (`tests/conftest.py`, `FakeApi`). With
`POLYMARKET_TEST_COVERAGE=strict` the run fails if some `Endpoint` was never called.
`tests/test_coverage.py` compares the endpoints with `tests/fixtures/api_manifest.json`; refresh
that file with `uv run --with pyyaml scripts/sync_api_manifest.py` when Polymarket publishes new
specifications, and add each new route to an `Endpoint` or to a table in `coverage.py`.

Signing code is tested against the official SDK's vectors in `tests/unit/test_signing_vectors.py`,
and `tests/oracle` compares with the SDK itself:

```bash
uv run --all-extras --with polymarket-client==0.18.0 pytest tests/oracle -m oracle
```

`tests/live` reads the real APIs and only runs with `POLYMARKET_LIVE=1`. It never writes. Some
Polymarket hosts block some networks; the `probe` and `live` workflows run from GitHub instead.
When a live run shows a response shape that differs from a fixture, update the fixture in the same
change.

Never commit a private key, API secret or session file, and never send a real order from a test.

Tool names, parameters, output fields, CLI flags and environment variables are the public API and
follow [Semantic Versioning](https://semver.org/). While the version is `0.y.z`, a minor release may
change them and a patch release will not. From `1.0.0`, removing or renaming one needs a major
release.
