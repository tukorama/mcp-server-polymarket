"""Gasless Deposit Wallet transactions: sign a batch of calls, submit it, follow it."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import ApiClient, encode_body
from mcp_server_polymarket.errors import (
    ConfigError,
    DeadlineExceeded,
    InvalidRequest,
    NetworkError,
    NotSent,
    PolymarketError,
    ServerError,
)
from mcp_server_polymarket.identity import Identity

logger = logging.getLogger(__name__)

DEADLINE_SECONDS = 600
TERMINAL = {"STATE_CONFIRMED", "STATE_FAILED", "STATE_INVALID"}
_NONCE_MISMATCH = re.compile(r"batch nonce\s+(\d+)\s+does not match on-chain nonce\s+(\d+)", re.I)
_locks: dict[str, asyncio.Lock] = {}
_sleep = asyncio.sleep
_now = time.time
_monotonic = time.monotonic


@dataclass
class WalletTx:
    transaction_id: str | None
    state: str | None
    transaction_hash: str | None = None
    error: str | None = None
    raw: Any = None
    # Set when the submit's outcome is unknown: the journal entry to resend.
    request_id: str | None = None


@dataclass
class Record:
    """How a batch is entered in the write journal, so it counts toward the daily limit and an
    unknown outcome can be resent byte for byte."""

    journal: Any
    tool: str
    summary: str
    usd: Any = None
    fingerprint: str | None = None


def _tx_fields(body: Any) -> WalletTx:
    body = body if isinstance(body, dict) else {}
    nested = body.get("transaction") if isinstance(body.get("transaction"), dict) else body
    return WalletTx(
        transaction_id=str(
            nested.get("transactionID") or nested.get("transaction_id") or nested.get("id") or ""
        )
        or None,
        state=nested.get("state"),
        transaction_hash=nested.get("transactionHash") or nested.get("transaction_hash"),
        error=nested.get("errorMsg") or nested.get("error_msg") or nested.get("error"),
        raw=body,
    )


def require_deposit_wallet(identity: Identity) -> tuple[str, str, str]:
    """(signer key, signer address, wallet) for a Deposit Wallet owner."""
    key, signer, wallet = identity.signer_key, identity.signer_address, identity.wallet_address
    if not key or not signer or not wallet:
        raise ConfigError(
            "Wallet transactions need the owner's key and wallet address.",
            hint="Set POLYMARKET_PRIVATE_KEY (or _FILE) and POLYMARKET_WALLET_ADDRESS.",
        )
    from mcp_server_polymarket.signing.wallets import WalletKind

    if identity.wallet_kind != WalletKind.DEPOSIT_WALLET:
        raise InvalidRequest(
            "Gasless transactions here support Deposit Wallets only.",
            hint="Safe and proxy wallets need the official SDK; EOA wallets pay their own gas.",
        )
    return key, signer, wallet


async def sign_batch(
    client: ApiClient,
    identity: Identity,
    calls: list[Any],
    *,
    deadline: float | None = None,
    nonce: int | None = None,
) -> dict[str, Any]:
    """Fetch the nonce and sign; returns the fields both /submit and session-key routes need."""
    from mcp_server_polymarket.signing import relayer

    key, signer, wallet = require_deposit_wallet(identity)
    if nonce is None:
        params = await client.json(
            ep.RELAYER_TX_PARAMS, params={"address": signer, "type": "WALLET"}, deadline=deadline
        )
        nonce = int((params or {}).get("nonce", 0))
    expires = int(_now()) + DEADLINE_SECONDS
    signature = relayer.sign_batch(
        key,
        wallet=wallet,
        calls=calls,
        nonce=nonce,
        deadline=expires,
        session_key=identity.is_session_key,
    )
    return {
        "signer": signer,
        "wallet": wallet,
        "nonce": nonce,
        "deadline": expires,
        "signature": signature,
    }


async def submit(
    client: ApiClient,
    identity: Identity,
    calls: list[Any],
    *,
    metadata: str = "",
    wait_seconds: float = 30,
    deadline: float | None = None,
    record: Record | None = None,
) -> WalletTx:
    """Sign and submit a batch, then follow it until it settles or `wait_seconds` pass.

    One batch at a time per wallet: they share a nonce. With `record`, the batch is journaled
    before it is sent; if the submit may have arrived without an answer, the result has state
    "UNKNOWN" and the entry's id, and it is never re-signed.
    """
    from mcp_server_polymarket.signing import relayer

    _, _, wallet = require_deposit_wallet(identity)
    lock = _locks.setdefault(wallet.lower(), asyncio.Lock())
    async with lock:
        signed = await sign_batch(client, identity, calls, deadline=deadline)
        body = relayer.batch_wire(calls=calls, metadata=metadata, **signed)
        outcome = await _post(client, body, deadline, record)
        if isinstance(outcome, PolymarketError):
            mismatch = _NONCE_MISMATCH.search(outcome.message)
            if not mismatch or int(mismatch.group(1)) >= int(mismatch.group(2)):
                raise outcome
            # The relayer saw a newer nonce; signing again with it cannot double-spend, since
            # the stale batch was rejected and never executes.
            signed = await sign_batch(
                client, identity, calls, deadline=deadline, nonce=int(mismatch.group(2))
            )
            body = relayer.batch_wire(calls=calls, metadata=metadata, **signed)
            outcome = await _post(client, body, deadline, record)
            if isinstance(outcome, PolymarketError):
                raise outcome
        if isinstance(outcome, WalletTx):
            return outcome
        response, entry = outcome
    tx = _tx_fields(response)
    if not tx.transaction_id:
        return tx
    tx = await follow(client, tx.transaction_id, wait_seconds=wait_seconds, deadline=deadline)
    if record is not None and entry is not None and tx.state in ("STATE_FAILED", "STATE_INVALID"):
        record.journal.finish(entry, "failed", {"state": tx.state, "error": tx.error})
    return tx


async def _post(
    client: ApiClient, body: Any, deadline: float | None, record: Record | None
) -> tuple[Any, str | None] | WalletTx | PolymarketError:
    """One submit. Returns (response, journal entry), an unknown WalletTx, or the definite
    error (returned rather than raised so the caller can re-sign on a nonce mismatch)."""
    raw = encode_body(body)
    entry = None
    if record is not None:
        entry = record.journal.record(
            tool=record.tool,
            endpoint=ep.RELAYER_SUBMIT.key,
            summary=record.summary,
            usd=record.usd,
            fingerprint=record.fingerprint,
            body=raw,
        )
    try:
        response = await client.json(ep.RELAYER_SUBMIT, raw_body=raw, deadline=deadline)
    except NotSent as exc:
        if entry is not None:
            record.journal.finish(entry, "failed", {"error": exc.message})  # type: ignore[union-attr]
        return exc
    except (NetworkError, ServerError, DeadlineExceeded) as exc:
        if entry is None:
            raise
        record.journal.finish(entry, "unknown", {"error": exc.message})  # type: ignore[union-attr]
        return WalletTx(transaction_id=None, state="UNKNOWN", error=exc.message, request_id=entry)
    except PolymarketError as exc:
        if entry is not None:
            record.journal.finish(entry, "failed", {"error": exc.message})  # type: ignore[union-attr]
        return exc
    except BaseException as exc:
        if entry is not None:
            record.journal.finish(entry, "unknown", {"error": f"interrupted: {type(exc).__name__}"})  # type: ignore[union-attr]
        raise
    if entry is not None:
        record.journal.finish(entry, "sent", response)  # type: ignore[union-attr]
    return response, entry


async def follow(
    client: ApiClient, transaction_id: str, *, wait_seconds: float, deadline: float | None = None
) -> WalletTx:
    started = _monotonic()
    delay = 1.0
    last = WalletTx(transaction_id=transaction_id, state="STATE_NEW")
    while True:
        body = await client.json(ep.RELAYER_TX, path_args={"id": transaction_id}, deadline=deadline)
        current = _tx_fields(body)
        current.transaction_id = current.transaction_id or transaction_id
        last = current
        if current.state in TERMINAL:
            return current
        remaining = wait_seconds - (_monotonic() - started)
        if deadline is not None:
            remaining = min(remaining, deadline - _monotonic() - 1)
        if remaining <= 0:
            return last
        await _sleep(min(delay, remaining))
        delay = min(delay * 1.5, 5)
