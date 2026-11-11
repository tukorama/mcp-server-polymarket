"""Signatures for Polymarket requests. Needs the `signing` extra (eth-account, eth-abi, msgpack).

Everything here is a pure function: keys, salts and timestamps come in as arguments, nothing
touches the network. Where the documentation is silent the formats follow the official SDK (see
NOTICE), and tests/unit/test_signing_vectors.py pins them to its golden vectors.
"""

from mcp_server_polymarket.errors import SigningUnavailable


def require() -> None:
    """Fail with installation advice when the signing extra is missing."""
    try:
        import eth_abi  # noqa: F401
        import eth_account  # noqa: F401
        import msgpack  # noqa: F401
    except ImportError as exc:
        raise SigningUnavailable() from exc
