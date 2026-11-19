from importlib import import_module

from mcp_server_polymarket.ops.registry import (
    OPERATIONS,
    Call,
    Operation,
    Runtime,
    enabled_operations,
    hidden_operations,
)

for _module in (
    "account",
    "analytics",
    "bridge",
    "builder",
    "combos",
    "discovery",
    "docs",
    "market_data",
    "perps_account",
    "perps_extras",
    "perps_market",
    "perps_trading",
    "rewards",
    "sports",
    "streams",
    "system",
    "trading",
    "wallet",
):
    import_module(f"{__name__}.{_module}")

__all__ = [
    "OPERATIONS",
    "Call",
    "Operation",
    "Runtime",
    "enabled_operations",
    "hidden_operations",
]
