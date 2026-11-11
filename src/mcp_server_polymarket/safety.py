"""Checks every write passes before anything is signed or sent, and the one way writes are sent.

The limits come from the real environment only (never .env). `0` blocks a kind of write;
`unlimited` lifts its limit.
"""

import hashlib
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import encode_body, parse_json
from mcp_server_polymarket.config import Limit
from mcp_server_polymarket.errors import (
    DeadlineExceeded,
    NetworkError,
    NotSent,
    PolymarketError,
    Refused,
    ServerError,
)
from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops.registry import Call

GEOBLOCK_TTL = 300


def check_limit(limit: Limit, amount: Decimal, what: str, setting: str) -> None:
    if limit == "unlimited":
        return
    if limit == 0:
        raise Refused(f"{what} is switched off ({setting}=0).")
    if amount > limit:
        raise Refused(
            f"{what} of ${amount:.2f} is above the limit of ${limit} ({setting}).",
            hint="Use a smaller amount, or ask the user to raise the limit.",
        )


def check_daily(call: Call, amount: Decimal, kind: str) -> None:
    limit = call.settings.max_daily_usd
    if limit == "unlimited":
        return
    spent = call.runtime.journal.spent_today(kind)
    if spent + amount > limit:
        raise Refused(
            f"This would bring today's {kind} to ${spent + amount:.2f}, above the daily limit of "
            f"${limit} (POLYMARKET_MAX_DAILY_USD).",
            hint="Wait until tomorrow (UTC), or ask the user to raise the limit.",
        )


def check_deviation(
    price: Decimal,
    reference: Decimal | None,
    side: str,
    what: str,
    *,
    relative: bool,
    limit: Limit | None = None,
) -> None:
    """Refuse prices far through the book: a typo should not sweep it."""
    if reference is None or limit is None or limit == "unlimited":
        return
    allowed = reference * limit if relative else limit
    if side == "BUY" and price > reference + allowed:
        raise Refused(
            f"{what} {price} is more than {allowed} above the best price {reference}.",
            hint="Check the price; raise POLYMARKET_MAX_PRICE_DEVIATION only if this is intended.",
        )
    if side == "SELL" and price < reference - allowed:
        raise Refused(
            f"{what} {price} is more than {allowed} below the best price {reference}.",
            hint="Check the price; raise POLYMARKET_MAX_PRICE_DEVIATION only if this is intended.",
        )


def check_recipient(call: Call, recipient: str, own: list[str | None]) -> None:
    """Funds may go to the user's own addresses, or to ones on POLYMARKET_TRANSFER_ALLOWLIST."""
    allowed = {a.lower() for a in own if a} | set(call.settings.transfer_allowlist)
    if recipient.lower() not in allowed:
        raise Refused(
            f"{recipient} is not one of the user's addresses or on the transfer allowlist.",
            hint="The user can add it to POLYMARKET_TRANSFER_ALLOWLIST.",
        )


async def check_geoblock(call: Call) -> None:
    """Order placement from restricted regions is refused here first, and fails closed."""
    # Kept on the runtime itself: an id() key could be reused by a later runtime.
    cached = call.runtime.geoblock
    if cached and time.monotonic() - cached[0] < GEOBLOCK_TTL:
        body = cached[1]
    else:
        try:
            body = await call.json(ep.WEB_GEOBLOCK)
        except PolymarketError as exc:
            raise Refused(
                "Could not confirm that this location may trade, so nothing was sent.",
                hint="Check get_service_status and retry.",
            ) from exc
        body = body if isinstance(body, dict) else {}
        call.runtime.geoblock = (time.monotonic(), body)
    if body.get("blocked") is not False:
        where = "-".join(str(p) for p in (body.get("country"), body.get("region")) if p)
        raise Refused(
            f"Polymarket does not allow trading from this location ({where or 'unknown'}).",
            hint="Do not try to get around this.",
        )


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:32]


def block_duplicate(call: Call, print_: str) -> None:
    previous = call.runtime.journal.pending_duplicate(print_)
    if previous is not None:
        raise Refused(
            f"An identical write ({previous.id}) has an unknown outcome.",
            hint=f"Check its result first, or send exactly the same request again with "
            f"resend_write(request_id='{previous.id}').",
        )


def unknown_result(entry: str, reason: str) -> WriteResult:
    return WriteResult(
        status="unknown",
        request_id=entry,
        message=f"The outcome is unknown: {reason}",
        next_step="Do not place it again. Check list_my_open_orders or the account, then "
        f"resend_write(request_id='{entry}') to send the identical request if it is missing.",
    )


async def send_write(
    call: Call,
    *,
    tool: str,
    endpoint: ep.Endpoint,
    body: Any,
    summary: str,
    usd: Decimal | None,
    print_: str | None,
    headers: dict[str, str] | None = None,
    rejected: Callable[[Any], bool] | None = None,
    path_args: dict[str, str] | None = None,
    resendable: bool = True,
) -> tuple[Any, WriteResult | None]:
    """Journal, then send. Returns (response, None), or (None, result) when the outcome is unknown.

    Definite rejections raise. A request that may have reached Polymarket is never repeated
    here; its bytes stay in the journal for resend_write, unless `resendable` is false (a
    request that is useless later, or whose path the journal cannot rebuild). `rejected`
    recognises a refusal that arrives as a 200 response, so it does not count toward the daily
    limit.
    """
    if path_args and resendable:
        raise ValueError("A write with path arguments cannot be resent; pass resendable=False.")
    journal = call.runtime.journal
    raw = encode_body(body) if body is not None else None
    entry = journal.record(
        tool=tool,
        endpoint=endpoint.key,
        summary=summary,
        usd=usd,
        fingerprint=print_,
        body=raw if resendable else None,
    )
    try:
        response = await call.request(endpoint, raw_body=raw, headers=headers, path_args=path_args)
    except NotSent as exc:
        journal.finish(entry, "failed", {"error": exc.message})
        raise
    except (NetworkError, ServerError, DeadlineExceeded) as exc:
        journal.finish(entry, "unknown", {"error": exc.message})
        result = unknown_result(entry, exc.message)
        if not resendable:
            result.next_step = (
                "Do not repeat it. Check the account to learn whether it went through."
            )
        return None, result
    except PolymarketError as exc:
        journal.finish(entry, "failed", {"error": exc.message})
        raise
    except BaseException as exc:
        # Cancelled or crashed mid-send: the request may have gone out, so treat it as unknown.
        journal.finish(entry, "unknown", {"error": f"interrupted: {type(exc).__name__}"})
        raise
    data = parse_json(response)
    journal.finish(entry, "rejected" if rejected is not None and rejected(data) else "sent", data)
    return data, None
