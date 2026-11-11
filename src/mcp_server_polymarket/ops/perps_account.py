"""The configured Perps account: balances, positions, orders, fills and history."""

import asyncio
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import describe_error
from mcp_server_polymarket.client.paging import epoch_ms
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, Section
from mcp_server_polymarket.ops.perps_market import perps_slim, window
from mcp_server_polymarket.ops.registry import Call, Cursor, When, operation

TOOLSET = "perps-account"
SESSION = ("perps_session",)


def _raw(body: Any) -> Raw:
    return Raw.model_validate(perps_slim(body) if isinstance(body, dict) else {"data": body})


def _page(body: Any) -> CursorPage[Raw]:
    body = body if isinstance(body, dict) else {"data": body}
    rows = body.get("data")
    rows = rows if isinstance(rows, list) else []
    return CursorPage(
        items=[Raw.model_validate(perps_slim(r)) for r in rows if isinstance(r, dict)],
        next_cursor=body.get("cursor") or body.get("next_cursor") or None,
        has_more=body.get("more", body.get("has_more")),
    )


def _times(start: str | None, end: str | None) -> dict[str, int]:
    params = {"start_timestamp": epoch_ms(start), "end_timestamp": epoch_ms(end)}
    return {k: v for k, v in params.items() if v is not None}


async def _sections(calls: dict[str, Any]) -> tuple[dict[str, Any], list[Section]]:
    names = list(calls)
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    values, errors = {}, []
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(Section(section=name, error=describe_error(result)))
        else:
            values[name] = perps_slim(result) if isinstance(result, dict | list) else result
    return values, errors


class PerpsAccount(CompactModel):
    credentials: Any = None
    stats: Any = None
    limits: Any = None
    auto_cancel: Any = None
    referral: Any = None
    instrument_config: Any = None
    errors: list[Section] | None = None


@operation("Get my Perps account", toolset=TOOLSET, requires=SESSION, read_only=True)
async def get_my_perps_account(
    call: Call,
    instrument_id: Annotated[
        int | None, Field(description="Also this instrument's leverage and margin mode.")
    ] = None,
) -> PerpsAccount:
    """Account address and its trading keys, 7-day volume and maker share, rate-limit tier and
    open-order cap, the auto-cancel (dead man's switch) state, referral code, and optionally an
    instrument's leverage and margin mode."""
    calls = {
        "credentials": call.json(ep.PERPS_CREDENTIALS),
        "stats": call.json(ep.PERPS_ACCOUNT_STATS),
        "limits": call.json(ep.PERPS_ACCOUNT_LIMITS),
        "auto_cancel": call.json(ep.PERPS_AUTO_CANCEL_STATUS),
        "referral": call.json(ep.PERPS_REFERRAL),
    }
    if instrument_id is not None:
        calls["instrument_config"] = call.json(
            ep.PERPS_ACCOUNT_CONFIG, params={"instrument_id": instrument_id}
        )
    values, errors = await _sections(calls)
    return PerpsAccount(**values, errors=errors or None)


class PerpsPortfolio(CompactModel):
    portfolio: Any = None
    balances: Any = None
    errors: list[Section] | None = None


@operation("Get my Perps portfolio", toolset=TOOLSET, requires=SESSION, read_only=True)
async def get_my_perps_portfolio(call: Call) -> PerpsPortfolio:
    """Equity, margin used, withdrawable balance, open positions with entry, mark, liquidation
    price and unrealised PnL, and asset balances."""
    values, errors = await _sections(
        {"portfolio": call.json(ep.PERPS_PORTFOLIO), "balances": call.json(ep.PERPS_BALANCES)}
    )
    return PerpsPortfolio(**values, errors=errors or None)


@operation("List my Perps orders", toolset=TOOLSET, requires=SESSION, read_only=True)
async def list_my_perps_orders(
    call: Call,
    open_only: Annotated[bool, Field(description="false: history incl. filled, cancelled.")] = True,
    instrument_id: int | None = None,
    order_id: int | None = None,
    client_order_id: str | None = None,
    start: When = None,
    end: When = None,
) -> CursorPage[Raw]:
    """Resting orders, or order history (latest state of each), up to 100 per call."""
    if open_only:
        only = {"instrument_id": instrument_id} if instrument_id is not None else None
        return _page(await call.json(ep.PERPS_OPEN_ORDERS, params=only))
    params: dict[str, Any] = _times(start, end)
    for key, value in (
        ("instrument_id", instrument_id),
        ("order_id", order_id),
        ("client_order_id", client_order_id),
    ):
        if value is not None:
            params[key] = value
    return _page(await call.json(ep.PERPS_ORDERS, params=params or None))


@operation("List my Perps fills", toolset=TOOLSET, requires=SESSION, read_only=True)
async def list_my_perps_fills(
    call: Call,
    instrument_id: int | None = None,
    start: When = None,
    end: When = None,
    oldest_first: bool = False,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Your fills with price, size, fee, liquidity side and realised PnL, 100 per page."""
    params: dict[str, Any] = {**_times(start, end), "sort": "asc" if oldest_first else "desc"}
    if instrument_id is not None:
        params["instrument_id"] = instrument_id
    if cursor:
        params["cursor"] = cursor
    return _page(await call.json(ep.PERPS_FILLS, params=params))


class History(CompactModel):
    series: Literal["equity", "pnl"]
    interval: str
    points: list[Any]
    more: bool | None = None


@operation("Get my Perps equity or PnL history", toolset=TOOLSET, requires=SESSION, read_only=True)
async def get_my_perps_history(
    call: Call,
    series: Annotated[
        Literal["equity", "pnl"],
        Field(description="equity: account value over time; pnl: PnL realised per bucket."),
    ] = "equity",
    interval: Literal["1m", "5m", "15m", "30m", "1h", "4h", "6h", "12h", "1d", "1w"] = "1d",
    start: When = None,
    end: When = None,
    count: Annotated[int, Field(ge=1, le=1000, description="Buckets when start is empty.")] = 30,
) -> History:
    """Account equity over time, or PnL realised in each bucket (PnL takes 1h, 4h, 1d, 1w)."""
    if series == "pnl" and interval not in ("1h", "4h", "1d", "1w"):
        raise InvalidRequest("PnL history takes interval 1h, 4h, 1d or 1w.")
    params: dict[str, Any] = {"interval": interval, **window(start, end, interval, count)}
    endpoint = ep.PERPS_EQUITY if series == "equity" else ep.PERPS_PNL
    body = await call.json(endpoint, params=params)
    body = body if isinstance(body, dict) else {"data": body}
    return History(
        series=series,
        interval=interval,
        points=perps_slim(body.get("data") or []),
        more=body.get("more"),
    )


@operation("List my Perps funding payments", toolset=TOOLSET, requires=SESSION, read_only=True)
async def list_my_perps_funding(
    call: Call,
    kind: Annotated[
        Literal["funding", "backstops"],
        Field(description="funding: payments on positions; backstops: liquidation transfers."),
    ] = "funding",
    instrument_id: int | None = None,
    start: When = None,
    end: When = None,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Funding paid or received on your positions, or cash moved by backstop liquidations."""
    params: dict[str, Any] = _times(start, end)
    if kind == "funding":
        if instrument_id is not None:
            params["instrument_id"] = instrument_id
        return _page(await call.json(ep.PERPS_FUNDING, params=params or None))
    if cursor:
        params["cursor"] = cursor
    return _page(await call.json(ep.PERPS_BACKSTOPS, params=params or None))


@operation("List my Perps transfers", toolset=TOOLSET, requires=SESSION, read_only=True)
async def list_my_perps_transfers(
    call: Call,
    kind: Literal["deposits", "withdrawals", "internal"] = "deposits",
    status: Annotated[
        str | None,
        Field(description="deposits: pending, confirmed, removed; withdrawals also failed."),
    ] = None,
    direction: Annotated[
        Literal["in", "out"] | None, Field(description="internal transfers only.")
    ] = None,
    tx_hash: str | None = None,
    start: When = None,
    end: When = None,
) -> CursorPage[Raw]:
    """Deposits into Perps, withdrawals out, or internal transfers between accounts."""
    params: dict[str, Any] = _times(start, end)
    if kind == "internal":
        if direction:
            params["direction"] = direction
        return _page(await call.json(ep.PERPS_INTERNAL_TRANSFERS, params=params or None))
    if tx_hash:
        params["hash"] = tx_hash
    if kind == "deposits":
        if status:
            params["deposit_status"] = status
        return _page(await call.json(ep.PERPS_DEPOSITS, params=params or None))
    if status:
        params["withdrawal_status"] = status
    return _page(await call.json(ep.PERPS_WITHDRAWALS, params=params or None))


@operation("Get my Perps liquidity rewards", toolset=TOOLSET, requires=SESSION, read_only=True)
async def get_my_perps_rewards(
    call: Call,
    date: Annotated[str | None, Field(description="One day, YYYY-MM-DD.")] = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> Raw:
    """Your daily liquidity reward shares per instrument (periods run 12:00 to 12:00 UTC)."""
    params: dict[str, Any] = {"limit": limit}
    for key, value in (("date", date), ("start_date", start_date), ("end_date", end_date)):
        if value:
            params[key] = value
    return _raw(await call.json(ep.PERPS_ACCOUNT_REWARDS, params=params))


@operation("List my Perps notifications", toolset=TOOLSET, requires=SESSION, read_only=True)
async def list_my_perps_notifications(
    call: Call,
    since_seq: Annotated[int | None, Field(description="Only newer than this sequence.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
    cursor: Cursor = None,
) -> Raw:
    """Account notifications (fills, liquidations, funding, deposits), newest first, with
    the unread state."""
    params: dict[str, Any] = {"limit": limit}
    if since_seq is not None:
        params["since_seq"] = since_seq
    if cursor:
        params["cursor"] = cursor
    return _raw(await call.json(ep.PERPS_NOTIFICATIONS, params=params))
