# 1. Domain and Safety Model

`mcp-server-polymarket` exposes **128 tools across 18 toolsets** covering Polymarket Predictions
(Gamma, CLOB, Data API v2, Relayer, Bridge, Combos, and WebSocket feeds) and Perps (HTTP and
WebSocket). Without credentials, only **49 read-only tools** are visible.

## Defense in Depth

1. **Tool Visibility Gates (`capabilities.py`, `ops/registry.py`)**
   - Every `Operation` declares `requires` (for example `clob_l2`, `relayer_key`, `perps_session`,
     `perps_signer`) and an optional `gate` (`cancel`, `trading`, `transfers`).
   - Writes stay completely hidden from `tools/list` unless explicitly enabled via
     `POLYMARKET_ALLOW_CANCEL`, `POLYMARKET_ALLOW_TRADING`, or `POLYMARKET_ALLOW_TRANSFERS`.
2. **Server-Authored Confirmation (`tools.py`, `safety.py`)**
   - Money-moving operations provide a dedicated `describe` coroutine that fetches authoritative
     market/instrument names, ticks, prices, and USD cost directly from API metadata rather than
     trusting model prose.
   - `tools.py` pauses execution via MCP Elicitation (`Confirmation`) before signing or broadcasting.
     Declining always returns a structured `WriteResult(status="declined")` without sending bytes.
     For `transfers`, `POLYMARKET_CONFIRM=none` is never honored — human confirmation is mandatory.
3. **Fail-Closed Preflight Checks (`ops/trading.py`, `ops/perps_trading.py`, `safety.py`)**
   - Geoblock verification (`safety.check_geoblock`) fails closed on network or API errors.
   - Off-tick prices are rejected (never silently rounded), share/collateral decimals are validated
     with `Decimal` arithmetic (`signing/orders.py`), price deviation from best bid/ask/mark is
     bounded (`POLYMARKET_MAX_PRICE_DEVIATION`), and per-order/daily USD limits are enforced.
   - Deposit Wallets, Proxy Wallets, Safe Wallets, EOA wallets, Protocol V1/V2 exchange routing, and
     signature types (`0`, `1`, `2`, `3`) are cross-checked against on-chain/derived addresses; any
     mismatch raises `ConfigError` instead of guessing.
4. **Secret Redaction & Prompt Hygiene (`redaction.py`, `config.py`)**
   - Secrets (`private_key`, CLOB API secrets/passphrases, HMAC signatures, Perps Ed25519 keys,
     bearer tokens) are registered via `register_secret` and replaced with `***` before logging or
     error rendering.
   - `.env` files are prevented from supplying keys or wallets (`Settings.load` / config isolation).
   - Untrusted external strings (market descriptions, comments, titles) pass through `clean_text`
     and `clean_label`, stripping C0/C1 control characters and Unicode bidirectional overrides.
