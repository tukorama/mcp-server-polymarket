# Releasing

Once, before the first release:

1. On PyPI, add a pending trusted publisher: project `mcp-server-polymarket`, owner `tukorama`,
   repository `mcp-server-polymarket`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository settings, create the `pypi` environment.
3. Optionally try a TestPyPI upload: `uv build --no-sources && uv publish --index testpypi`.

Every release:

1. Run the `live` workflow (Actions → live → Run workflow) and check that it passes.
2. `uv version --bump patch` (or `minor`, `major`), and set the same version in `server.json`.
3. Date the entry in `CHANGELOG.md`, commit and push, and wait for CI to pass.
4. `git tag -a v$(uv version --short) -m v$(uv version --short) && git push --follow-tags`.

The release workflow uploads to PyPI, then publishes `server.json` to the MCP Registry and creates
the GitHub release. If only the registry job fails, use "Re-run failed jobs". If the manifest itself
was wrong, fix it on `main` and publish by hand: `mcp-publisher login github && mcp-publisher publish`.
The registry and PyPI never accept the same version twice, so a fix to either needs a new version.
`mcp-publisher` is pinned by version and checksum in both workflows; update the two together.
