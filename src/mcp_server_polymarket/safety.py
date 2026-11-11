"""Checks every write passes before anything is signed or sent, and the one way writes are sent.

The limits come from the real environment only (never .env). `0` blocks a kind of write;
`unlimited` lifts its limit.
"""

import hashlib
import time
from decimal import Decimal
from typing import Any

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import encode_body, parse_json
from mcp_server_polymarket.config import Limit
from mcp_server_polymarket.errors import (
    DeadlineExceeded,
    NetworkError,
    PolymarketError,
    Refused,
    ServerError,
)
from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops.registry import Call

GEOBLOCK_TTL = 300
_geoblock_cache: dict[int, tuple[float, dict[str, Any]]] = {}


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
    key = id(call.runtime)
    cached = _geoblock_cache.get(key)
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
        _geoblock_cache[key] = (time.monotonic(), body)
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
) -> tuple[Any, WriteResult | None]:
    """Journal, then send. Returns (response, None), or (None, result) when the outcome is unknown.

    Definite rejections raise. A request that may have reached Polymarket is never repeated
    here; its bytes stay in the journal for resend_write.
    """
    journal = call.runtime.journal
    raw = encode_body(body) if body is not None else None
    entry = journal.record(
        tool=tool, endpoint=endpoint.key, summary=summary, usd=usd, fingerprint=print_, body=raw
    )
    try:
        response = await call.request(endpoint, raw_body=raw, headers=headers)
    except (NetworkError, ServerError, DeadlineExceeded) as exc:
        journal.finish(entry, "unknown", {"error": exc.message})
        return None, WriteResult(
            status="unknown",
            request_id=entry,
            message=f"The outcome is unknown: {exc.message}",
            next_step="Do not place it again. Check list_my_open_orders or the account, then "
            f"resend_write(request_id='{entry}') to send the identical request if it is missing.",
        )
    except PolymarketError as exc:
        journal.finish(entry, "failed", {"error": exc.message})
        raise
    data = parse_json(response)
    journal.finish(entry, "sent", data)
    return data, None
