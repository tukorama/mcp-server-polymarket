# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[SemVer 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-11

### Added

- MCP server with 128 tools in 18 toolsets covering Polymarket Predictions (Gamma, CLOB, Data API
  v2, Relayer, Bridge, Combos, WebSocket feeds) and Perps (HTTP and WebSocket); 49 without
  credentials.
- Local signing in the `signing` extra: CLOB orders (exchange versions 2 and 3, EOA, Proxy, Safe,
  Deposit Wallet and session keys), Deposit Wallet batches and Perps operations.
- Write safety: environment gates, user confirmation through elicitation, USD and leverage
  limits, price-sanity checks, recipient allowlist, geoblock check and a write journal with
  byte-identical resend.
- CLI: `serve`, `auth` (CLOB, Perps, session keys, builder keys), `logout`, `status`, `tools`.
- Native LangChain tools and a prebuilt LangGraph agent with interrupts for writes, in the
  `langgraph` extra.
- Stdio and authenticated streamable HTTP transports.
- An endpoint manifest generated from the published specifications, with a test that every
  endpoint is covered or waived with a reason.

[Unreleased]: https://github.com/tukorama/mcp-server-polymarket/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tukorama/mcp-server-polymarket/releases/tag/v0.1.0
