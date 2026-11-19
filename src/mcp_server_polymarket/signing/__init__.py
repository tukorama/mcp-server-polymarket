"""Signatures for Polymarket requests. Needs the `signing` extra (eth-account, eth-abi, msgpack).

Everything here is a pure function: keys, salts and timestamps come in as arguments, nothing
touches the network. Where the documentation is silent the formats follow the official SDK (see
NOTICE), and tests/unit/test_signing_vectors.py pins them to its golden vectors.
"""

from importlib import import_module

from mcp_server_polymarket.errors import SigningUnavailable


def require() -> None:
    """Fail with installation advice when the signing extra is missing."""
    try:
        for package in ("eth_abi", "eth_account", "msgpack"):
            import_module(package)
    except ImportError as exc:
        raise SigningUnavailable from exc
