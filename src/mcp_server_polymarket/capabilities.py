"""What this configuration can do, decided once at startup.

Every place that lists tools (the MCP server, the CLI, `auth_status` and LangGraph) uses the same
snapshot and the same `visible` function, so they cannot disagree.
"""

from dataclasses import dataclass, field

from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import ConfigError
from mcp_server_polymarket.identity import Identity, signing_available

# Requirement name -> how to get it, shown when a tool is hidden.
REQUIREMENTS = {
    "clob_l2": "CLOB API credentials: run `mcp-server-polymarket auth` or set POLYMARKET_API_KEY, "
    "POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE and POLYMARKET_ADDRESS",
    "signer": "a signing key: set POLYMARKET_PRIVATE_KEY (or _FILE) and POLYMARKET_WALLET_ADDRESS, "
    "with the `signing` extra installed",
    "owner_signer": "the wallet owner's key (POLYMARKET_SIGNER_ROLE=owner)",
    "relayer_key": "a Relayer API key: POLYMARKET_RELAYER_API_KEY and "
    "POLYMARKET_RELAYER_API_KEY_ADDRESS",
    "perps_session": "a Perps session: run `mcp-server-polymarket auth --perps`",
    "perps_signer": "a Perps session that can sign: `mcp-server-polymarket auth --perps` without "
    "--read-only",
    "perps_owner": "the Perps account owner's key: POLYMARKET_PRIVATE_KEY with "
    "POLYMARKET_SIGNER_ROLE=owner",
}

GATES = {
    "cancel": "POLYMARKET_ALLOW_CANCEL=true (or POLYMARKET_ALLOW_TRADING=true)",
    "trading": "POLYMARKET_ALLOW_TRADING=true",
    "transfers": "POLYMARKET_ALLOW_TRANSFERS=true",
}

TOOLSETS = {
    "discovery": "Find events, markets, tags, series, comments and profiles (Gamma)",
    "market-data": "Order books, quotes and price history",
    "analytics": "Positions, trades, activity, holders and leaderboards (Data API)",
    "perps-market": "Perps instruments, books, candles, trades and funding",
    "system": "Service status and what this server can do",
    "docs": "Search and read the Polymarket documentation",
    "account": "Your CLOB orders, trades, balances and relayer transactions",
    "perps-account": "Your Perps balances, orders, fills and history",
    "trading": "Place and cancel prediction market orders",
    "wallet": "Approvals, split, merge and redeem through the Relayer",
    "perps-trading": "Perps orders, leverage, margin, deposits and withdrawals",
    "sports": "Sports metadata, teams, results and live scores",
    "rewards": "Liquidity rewards, rebates and order scoring",
    "streams": "Short WebSocket captures of books, prices and Perps markets",
    "bridge": "Cross-chain deposits and withdrawals",
    "combos": "Multi-leg Combo quotes and collateral return",
    "builder": "Builder program boards, trades, keys and fee approvals",
    "perps-extras": "Perps leaderboard, competitions, share cards and BLP",
}
DEFAULT_TOOLSETS = frozenset(
    {
        "discovery",
        "market-data",
        "analytics",
        "perps-market",
        "system",
        "docs",
        "account",
        "perps-account",
        "trading",
        "wallet",
        "perps-trading",
    }
)
_META = {
    "auto": DEFAULT_TOOLSETS,
    "all": frozenset(TOOLSETS),
    "perps": frozenset(name for name in TOOLSETS if name.startswith("perps")),
    "predictions": frozenset(name for name in TOOLSETS if not name.startswith("perps")) - {"docs"},
}


def parse_toolsets(spec: str) -> frozenset[str]:
    """`auto`, `all`, `predictions`, `perps`, names, and `+name` / `-name` adjustments."""
    selected: set[str] = set()
    tokens = [token.strip() for token in spec.split(",") if token.strip()] or ["auto"]
    if all(token[0] in "+-" for token in tokens):
        tokens.insert(0, "auto")
    for token in tokens:
        sign, name = (token[0], token[1:]) if token[0] in "+-" else ("+", token)
        names = _META.get(name) or ({name} if name in TOOLSETS else None)
        if names is None:
            known = ", ".join([*_META, *TOOLSETS])
            raise ConfigError(f"Unknown toolset {name!r} in POLYMARKET_TOOLSETS. Known: {known}.")
        if sign == "+":
            selected |= names
        else:
            selected -= names
    return frozenset(selected)


@dataclass(frozen=True)
class Capabilities:
    toolsets: frozenset[str]
    have: frozenset[str]
    gates: frozenset[str]
    notes: tuple[str, ...] = field(default=())

    @classmethod
    def detect(cls, settings: Settings, identity: Identity) -> "Capabilities":
        have: set[str] = set()
        notes: list[str] = []
        try:
            if identity.clob_credentials() is not None:
                have.add("clob_l2")
        except ConfigError as exc:
            notes.append(exc.message)
        signer = identity.signer_address is not None and identity.wallet_address is not None
        if identity.signer_address is not None and identity.wallet_address is None:
            notes.append("POLYMARKET_WALLET_ADDRESS is needed before anything can be signed.")
        if signer:
            try:
                _ = identity.wallet_kind
            except ConfigError as exc:
                notes.append(exc.for_agent())
                signer = False
        if signer:
            have.add("signer")
            if settings.signer_role == "owner":
                have.add("owner_signer")
        if identity.relayer_key() is not None:
            have.add("relayer_key")
        session = identity.perps_session()
        if session is not None:
            have.add("perps_session")
            if session.can_sign and signing_available():
                have.add("perps_signer")
        if identity.signer_address is not None and settings.signer_role == "owner":
            have.add("perps_owner")
        gates: set[str] = set()
        if settings.allow_cancel or settings.allow_trading:
            gates.add("cancel")
        if settings.allow_trading:
            gates.add("trading")
        if settings.allow_transfers:
            gates.add("transfers")
        notes.extend(identity.notes)
        return cls(
            toolsets=parse_toolsets(settings.toolsets),
            have=frozenset(have),
            gates=frozenset(gates),
            notes=tuple(dict.fromkeys(notes)),
        )


def visible(
    toolset: str, requires: tuple[str, ...], gate: str | None, caps: Capabilities
) -> tuple[bool, str]:
    """Whether a tool is exposed, and if not, the step that would expose it."""
    if toolset not in caps.toolsets:
        return False, f"add `{toolset}` to POLYMARKET_TOOLSETS (e.g. `auto,+{toolset}`)"
    if gate is not None and gate not in caps.gates:
        return False, f"set {GATES[gate]}"
    missing = [name for name in requires if name not in caps.have]
    if missing:
        return False, "needs " + "; ".join(REQUIREMENTS[name] for name in missing)
    return True, ""
