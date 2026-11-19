# 3. State Machine and Verification

Financial writes cannot be blindly retried over HTTP. This project enforces multiple automated
correctness gates locally and in CI.

## SQLite Write Journal Finite-State Machine (`journal.py`)

Before any order, transfer, or batch transaction leaves the host, `Journal.record` writes an entry
in `pending` state to SQLite with POSIX `0o600` file permissions.

- **Allowed transitions (`WRITE_TRANSITIONS`):**
  - `None -> pending`
  - `pending -> {sent, rejected, failed, unknown, cleared}`
  - `unknown -> {sent, resend_rejected, failed, cleared}`
  - Terminal states `{sent, rejected, failed, resend_rejected, cleared}` accept zero further
    transitions; `validate_transition` raises `ValueError` on illegal jumps.
- **Ambiguous outcomes & resend safety:**
  - When a request times out or disconnects after bytes leave the socket, `Journal.finish(..., "unknown")`
    retains the exact signed `body` for up to `BODY_TTL_SECONDS` (900 seconds).
  - Identical fingerprints (`BLOCKING = ("pending", "unknown", "resend_rejected")`) block new
    accidental duplicates until the user inspects `list_unconfirmed_writes`, resends byte-for-byte
    via `resend_write`, or clears it via `Journal.clear_unknown`.
  - Daily spend (`SPENDING = ("pending", "sent", "unknown", "resend_rejected")`) counts committed
    or ambiguous writes against `POLYMARKET_MAX_DAILY_USD`.

## Automated Verification Suite

1. **Property & Stateful Testing (`tests/unit/test_invariants.py`)**
   - `WriteJournalStateMachine` uses Hypothesis `RuleBasedStateMachine` to fuzz sequences of
     writes, ambiguous outcomes, manual clears, terminal rejection attempts, spend totals, and
     duplicate blocking.
   - Property tests validate `validate_transition`, `as_decimal_str` round-trips, converter safety,
     and `scrub` / `clean_text` hygiene.
2. **Static Analysis & Typing (`pyproject.toml`)**
   - **Ruff** enforces strict lint rules (`E, F, I, UP, B, SIM, RUF, S, ASYNC, PT, C90, PIE, PERF,
     RET, RSE, TID, PGH, PLC, PLE`) with McCabe complexity capped at `max-complexity = 12`.
   - **Pyright** runs in `typeCheckingMode = "strict"` across both `src` and `tests` with zero
     diagnostics allowed.
3. **Architecture & Secret Scanning (`tests/test_coverage.py`, `.gitleaks.toml`)**
   - AST verification ensures the 6-layer import DAG has zero upward edges.
   - Endpoint coverage verification (`POLYMARKET_TEST_COVERAGE=strict`) fails if any published route
     or client `Endpoint` lacks test coverage or an explicit entry in `coverage.py`.
   - `.gitleaks.toml` checks default rules plus Polymarket EVM key, Ed25519 key, and HMAC secret
     patterns across source and documentation.
4. **SDK Signing Oracle (`tests/oracle/test_against_sdk.py`)**
   - Compares EIP-712 digests, order amounts, Deposit Wallet batches, and Perps command hashing
     directly against the upstream `polymarket-client` SDK in CI.
