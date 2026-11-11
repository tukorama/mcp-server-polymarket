"""CLOB L1 authentication: the wallet proves control to create or derive API credentials."""

from typing import Any

from mcp_server_polymarket.signing.eip712 import CHAIN_ID, address_of, sign

_MESSAGE = "This message attests that I control the given wallet"


def clob_auth_typed_data(address: str, timestamp: int, nonce: int = 0) -> dict[str, Any]:
    return {
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID},
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "message": {
            "address": address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": _MESSAGE,
        },
    }


def l1_headers(private_key: str, timestamp: int, nonce: int = 0) -> dict[str, str]:
    """Headers for POST /auth/api-key and GET /auth/derive-api-key."""
    address = address_of(private_key)
    signature = sign(private_key, clob_auth_typed_data(address, timestamp, nonce))
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }
