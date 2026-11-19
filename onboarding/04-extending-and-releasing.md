# 4. Extending and Releasing

## Adding a New Tool or Endpoint

1. **Declare the `Endpoint`** in `src/mcp_server_polymarket/client/endpoints.py` (every HTTP call must
   reference an `Endpoint` constant so `POLYMARKET_TEST_COVERAGE=strict` tracks it). If an
   upstream OpenAPI endpoint is intentionally skipped, document it in
   `src/mcp_server_polymarket/coverage.py`.
2. **Respect layer boundaries**: add response models in `models/` (Layer 1), client helpers in
   `client/` (Layer 2), and the `@operation(...)` handler in `ops/` (Layer 3).
3. **Write a `describe` function for writes**: if the operation changes positions, approves
   contracts, or moves funds, set `gate` and `describe=...`, return a subclass of `WriteResult`,
   and keep any `signing` imports local to the execution path so `base-install` stays clean.
4. **Add scripted tests** in `tests/mcp/` using `FakeApi` (`tests/conftest.py`) and verify:

   ```bash
   uv sync --locked --all-extras
   uv run ruff check . && uv run ruff format --check .
   uv run pyright
   POLYMARKET_TEST_COVERAGE=strict uv run pytest -v
   ```

5. Keep test functions annotated (`-> None` / `-> Any`) and avoid adding runtime secrets or live
   network dependencies to non-`live` tests.

## Refreshing API Manifests

When Polymarket publishes specification updates:

```bash
uv run --with pyyaml scripts/sync_api_manifest.py
POLYMARKET_TEST_COVERAGE=strict uv run pytest tests/test_coverage.py
```

## Cutting a Release

1. Update the version string consistently across `pyproject.toml`, `server.json`, and
   `CHANGELOG.md`.
2. Verify lockfile, build, wheel smoke test, and security audit locally:

   ```bash
   uv lock --check
   uv run pip-audit
   rm -rf dist && uv build --no-sources
   uvx twine check dist/*
   uvx --from dist/*.whl mcp-server-polymarket tools
   ```

3. Commit, push to `main`, verify `.github/workflows/ci.yml` passes (including `lint`, `security`,
   `test` matrix, `base-install`, `oracle`, and `package`), then push the matching `vX.Y.Z` tag.
   `.github/workflows/release.yml` automatically builds, signs PEP 740 attestations, runs
   `uv publish` to PyPI via Trusted Publishing, updates the MCP Registry, and creates the GitHub
   Release.
