"""Request authentication headers. Only the standard library: HMAC needs no signing extra."""

import base64
import hashlib
import hmac
from dataclasses import dataclass, field


def _decode_secret(secret: str) -> bytes:
    # Polymarket issues URL-safe base64 secrets, sometimes without padding.
    padded = secret + "=" * (-len(secret) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def hmac_signature(secret: str, timestamp: int, method: str, path: str, body: bytes | None) -> str:
    """URL-safe base64 HMAC-SHA256 over timestamp + METHOD + path + body.

    The path excludes the query string, and the body is the exact bytes sent.
    """
    message = f"{timestamp}{method.upper()}{path}".encode()
    if body:
        message += body
    digest = hmac.new(_decode_secret(secret), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


@dataclass(frozen=True)
class HmacKey:
    api_key: str
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)


def clob_l2_headers(
    key: HmacKey, address: str, timestamp: int, method: str, path: str, body: bytes | None
) -> dict[str, str]:
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": hmac_signature(key.secret, timestamp, method, path, body),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": key.api_key,
        "POLY_PASSPHRASE": key.passphrase,
    }


def builder_headers(
    key: HmacKey, timestamp: int, method: str, path: str, body: bytes | None
) -> dict[str, str]:
    return {
        "POLY_BUILDER_API_KEY": key.api_key,
        "POLY_BUILDER_TIMESTAMP": str(timestamp),
        "POLY_BUILDER_PASSPHRASE": key.passphrase,
        "POLY_BUILDER_SIGNATURE": hmac_signature(key.secret, timestamp, method, path, body),
    }


def relayer_headers(api_key: str, address: str) -> dict[str, str]:
    return {"RELAYER_API_KEY": api_key, "RELAYER_API_KEY_ADDRESS": address}


def perps_headers(proxy: str, secret: str) -> dict[str, str]:
    return {"POLYMARKET-PROXY": proxy, "POLYMARKET-SECRET": secret}
