import asyncio
import time
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import describe_error
from mcp_server_polymarket.client.cache import MINUTE
from mcp_server_polymarket.client.paging import epoch_ms
from mcp_server_polymarket.errors import InvalidRequest, NotFound
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, Section, iso_time
from mcp_server_polymarket.ops.registry import Call, Cursor, InstrumentId, When, operation

TOOLSET = "perps-market"
Interval = Literal["1s", "1m", "5m", "15m", "30m", "1h", "4h", "6h", "12h", "1d", "1w"]
INTERVAL_SECONDS = {
    "1s": 1,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}
_TIME_KEYS = ("timestamp", "next_funding", "ui_live_time", "time", "ts", "deadline")
_DROP = {"logo"}


def perps_slim(value: Any) -> Any:
    """Perps answers in decimal strings and millisecond times; make the times readable."""
    if isinstance(value, list):
        return [perps_slim(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _DROP or item is None:
            continue
        timed = key in _TIME_KEYS or key.endswith(("_at", "_ts", "_time", "_timestamp"))
        if isinstance(item, int) and not isinstance(item, bool) and timed and item > 10**9:
            item = iso_time(item) or item
        elif isinstance(item, dict | list):
            item = perps_slim(item)
        result[key] = item
    return result


def now_ms() -> int:
    return int(time.time() * 1000)


def window(
    start: str | None, end: str | None, interval: str | None = None, count: int = 100
) -> dict[str, int]:
    """start/end in ms. Without a start, look back `count` intervals (or a day)."""
    end_ms = epoch_ms(end) if end else None
    start_ms = epoch_ms(start) if start else None
    if start_ms is None:
        span = INTERVAL_SECONDS.get(interval or "", 864) * count * 1000
        start_ms = (end_ms or now_ms()) - span
    params = {"start_timestamp": start_ms}
    if end_ms is not None:
        params["end_timestamp"] = end_ms
    return params


async def instruments(call: Call) -> list[dict[str, Any]]:
    cache = call.runtime.cache
    cached = cache.get("perps:instruments", 5 * MINUTE)
    if cached is not None:
        return cached
    rows = await call.json(ep.PERPS_INSTRUMENTS)
    rows = [r for r in rows or [] if isinstance(r, dict)]
    cache.set("perps:instruments", rows, disk=False)
    return rows


async def instrument(call: Call, instrument_id: int) -> dict[str, Any]:
    for row in await instruments(call):
        if row.get("instrument_id") == instrument_id:
            return row
    raw = await call.json(ep.PERPS_INSTRUMENTS, params={"instrument_id": instrument_id})
    rows = raw if isinstance(raw, list) else []
    if rows and isinstance(rows[0], dict):
        return rows[0]
    raise NotFound(
        f"No Perps instrument {instrument_id}.",
        hint="Find ids with list_perps_instruments.",
    )


class ExchangeInfo(CompactModel):
    exchange: dict[str, Any] | None = None
    collateral_assets: list[Raw] | None = None
    fee_schedule: list[Raw] | None = None
    limit_tiers: list[Raw] | None = None
    errors: list[Section] | None = None


@operation("Get Perps exchange info", toolset=TOOLSET, read_only=True)
async def get_perps_exchange_info(call: Call) -> ExchangeInfo:
    """The Perps exchange: chain and settlement contract, whether it is cancel-only, collateral
    assets and withdrawal fees, the maker/taker fee tiers, and account rate-limit tiers."""
    names = ("exchange", "assets", "fees", "limit_tiers")
    results = await asyncio.gather(
        call.json(ep.PERPS_EXCHANGE),
        call.json(ep.PERPS_ASSETS),
        call.json(ep.PERPS_FEES),
        call.json(ep.PERPS_LIMIT_TIERS),
        return_exceptions=True,
    )
    errors = [
        Section(section=name, error=describe_error(result))
        for name, result in zip(names, results, strict=True)
        if isinstance(result, BaseException)
    ]
    exchange, assets, fees, tiers = (None if isinstance(r, BaseException) else r for r in results)

    def listed(value: Any, key: str | None = None) -> list[Raw] | None:
        if isinstance(value, dict) and key:
            value = value.get(key)
        if not isinstance(value, list):
            return None
        return [Raw.model_validate(perps_slim(v)) for v in value if isinstance(v, dict)]

    return ExchangeInfo(
        exchange=exchange if isinstance(exchange, dict) else None,
        collateral_assets=listed(assets, "assets") or listed(assets),
        fee_schedule=listed(fees, "fee_schedule"),
        limit_tiers=listed(tiers, "tiers") or listed(tiers),
        errors=errors or None,
    )


class Instrument(Raw):
    instrument_id: int
    symbol: str | None = None
    category: str | None = None
    max_leverage: int | None = None


class Instruments(CompactModel):
    instruments: list[Instrument]


@operation("List Perps instruments", toolset=TOOLSET, read_only=True)
async def list_perps_instruments(
    call: Call,
    category: Literal["crypto", "equity", "index", "commodity", "pre_ipo"] | None = None,
    search: Annotated[str | None, Field(description="Substring of the symbol, e.g. BTC.")] = None,
    instrument_id: Annotated[
        int | None, Field(description="One instrument, including a settled one.")
    ] = None,
    include_risk_tiers: bool = False,
) -> Instruments:
    """Listed perpetual markets: id, symbol, category, decimals, leverage, notional limits and
    funding interval. Every other Perps tool takes the integer instrument_id from here."""
    if instrument_id is not None:
        raw = await call.json(ep.PERPS_INSTRUMENTS, params={"instrument_id": instrument_id})
        rows = raw if isinstance(raw, list) else []
    else:
        rows = await instruments(call)
    needle = search.lower() if search else None
    result = []
    for row in rows:
        if category and row.get("category") != category:
            continue
        symbols = (
            f"{row.get('symbol', '')} {row.get('display_symbol', '')} {row.get('base_asset', '')}"
        )
        if needle and needle not in symbols.lower():
            continue
        slimmed = perps_slim(row)
        if not include_risk_tiers:
            slimmed.pop("risk_tiers", None)
        result.append(Instrument.model_validate(slimmed))
    return Instruments(instruments=result)


class Ticker(Raw):
    instrument_id: int
    symbol: str | None = None


class Tickers(CompactModel):
    tickers: list[Ticker]
    errors: list[Section] | None = None


@operation("Get Perps tickers", toolset=TOOLSET, read_only=True)
async def get_perps_tickers(
    call: Call,
    instrument_id: Annotated[int | None, Field(description="Empty means every instrument.")] = None,
    include_statistics: Annotated[bool, Field(description="Add 24h volume, range, change.")] = True,
    include_bbo: Annotated[bool, Field(description="Add best bid and ask with sizes.")] = False,
) -> Tickers:
    """Mark, index, last and mid prices, open interest, funding rate and next funding time,
    merged with 24h statistics and optionally the best bid and offer."""
    params = {"instrument_id": instrument_id} if instrument_id is not None else None
    calls = [call.json(ep.PERPS_TICKERS, params=params)]
    if include_statistics:
        calls.append(call.json(ep.PERPS_STATISTICS, params=params))
    if include_bbo:
        calls.append(call.json(ep.PERPS_BBO, params=params))
    results = await asyncio.gather(*calls, return_exceptions=True)
    if isinstance(results[0], BaseException):
        raise results[0]
    merged: dict[int, dict[str, Any]] = {}
    for row in results[0] or []:
        if isinstance(row, dict) and row.get("instrument_id") is not None:
            merged[row["instrument_id"]] = dict(row)
    errors = []
    extras = []
    if include_statistics:
        extras.append(("statistics", results[1]))
    if include_bbo:
        extras.append(("bbo", results[-1]))
    for name, result in extras:
        if isinstance(result, BaseException):
            errors.append(Section(section=name, error=describe_error(result)))
            continue
        for row in result or []:
            if not isinstance(row, dict) or row.get("instrument_id") not in merged:
                continue
            target = merged[row["instrument_id"]]
            for key, value in row.items():
                if key in ("instrument_id", "symbol", "klines"):
                    continue
                target.setdefault(key if key not in target else f"{name}_{key}", value)
    return Tickers(
        tickers=[Ticker.model_validate(perps_slim(row)) for row in merged.values()],
        errors=errors or None,
    )


class PerpsLevel(CompactModel):
    price: str
    quantity: str


class PerpsBook(CompactModel):
    instrument_id: int
    best_bid: str | None = None
    best_ask: str | None = None
    bids: list[PerpsLevel]
    asks: list[PerpsLevel]
    timestamp: str | None = None
    sequence: int | None = None


@operation("Get a Perps order book", toolset=TOOLSET, read_only=True)
async def get_perps_book(
    call: Call,
    instrument_id: InstrumentId,
    depth: Annotated[Literal[10, 100, 500, 1000], Field(description="Levels per side.")] = 10,
) -> PerpsBook:
    """A Perps order book, best levels first. Prices in pUSD, quantities in base units."""
    raw = await call.json(ep.PERPS_BOOK, params={"instrument_id": instrument_id, "depth": depth})
    raw = raw if isinstance(raw, dict) else {}

    def levels(side: Any) -> list[PerpsLevel]:
        return [
            PerpsLevel(price=str(level[0]), quantity=str(level[1]))
            for level in side or []
            if isinstance(level, list | tuple) and len(level) >= 2
        ]

    bids, asks = levels(raw.get("bids")), levels(raw.get("asks"))
    return PerpsBook(
        instrument_id=instrument_id,
        best_bid=bids[0].price if bids else None,
        best_ask=asks[0].price if asks else None,
        bids=bids,
        asks=asks,
        timestamp=iso_time(raw.get("timestamp")),
        sequence=raw.get("sequence"),
    )


class Candle(CompactModel):
    time: str
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    volume: str | None = None
    trades: int | None = None


class Candles(CompactModel):
    instrument_id: int
    interval: str
    kind: str
    candles: list[Candle]
    more: bool | None = None


@operation("Get Perps candles", toolset=TOOLSET, read_only=True)
async def get_perps_candles(
    call: Call,
    instrument_id: InstrumentId,
    interval: Interval = "1h",
    kind: Annotated[
        Literal["trades", "mark"], Field(description="trades: OHLCV of trades; mark: mark price.")
    ] = "trades",
    start: Annotated[When, Field(description="Empty looks back `count` intervals.")] = None,
    end: When = None,
    count: Annotated[int, Field(ge=1, le=1000, description="Candles when start is empty.")] = 100,
) -> Candles:
    """Price candles for an instrument, oldest first. With kind=mark, `close` is the mark price
    at the end of each bucket."""
    params = {"instrument_id": instrument_id, "interval": interval}
    params |= window(start, end, interval, count)
    endpoint = ep.PERPS_KLINES if kind == "trades" else ep.PERPS_MARK_HISTORY
    raw = await call.json(endpoint, params=params)
    raw = raw if isinstance(raw, dict) else {"data": raw}
    candles = []
    for row in raw.get("data") or []:
        if not isinstance(row, list | tuple) or not row:
            continue
        if kind == "mark":
            candles.append(Candle(time=iso_time(row[0]) or str(row[0]), close=str(row[1])))
            continue
        values = list(row) + [None] * 7
        candles.append(
            Candle(
                time=iso_time(values[0]) or str(values[0]),
                open=None if values[1] is None else str(values[1]),
                high=None if values[2] is None else str(values[2]),
                low=None if values[3] is None else str(values[3]),
                close=None if values[4] is None else str(values[4]),
                volume=None if values[5] is None else str(values[5]),
                trades=values[6],
            )
        )
    return Candles(
        instrument_id=instrument_id,
        interval=interval,
        kind=kind,
        candles=candles,
        more=raw.get("more"),
    )


@operation("Get a Perps index price", toolset=TOOLSET, read_only=True)
async def get_perps_index(
    call: Call, asset: Annotated[str, Field(description="Base asset, e.g. BTC or SP500.")]
) -> Raw:
    """The index price of an asset and the constituent prices and weights it is built from."""
    raw = await call.json(ep.PERPS_INDEX, params={"asset": asset})
    return Raw.model_validate(perps_slim(raw) if isinstance(raw, dict) else {"data": raw})


def _page(raw: Any) -> CursorPage[Raw]:
    raw = raw if isinstance(raw, dict) else {"data": raw}
    items = [
        Raw.model_validate(perps_slim(r)) for r in raw.get("data") or [] if isinstance(r, dict)
    ]
    return CursorPage(items=items, next_cursor=raw.get("cursor") or None, has_more=raw.get("more"))


@operation("List recent Perps trades", toolset=TOOLSET, read_only=True)
async def list_perps_trades(
    call: Call,
    instrument_id: InstrumentId,
    start: When = None,
    end: When = None,
) -> CursorPage[Raw]:
    """Public trades of an instrument, newest first, up to 100 per call. Narrow the window with
    start and end to go further back."""
    params: dict[str, Any] = {"instrument_id": instrument_id}
    if start:
        params["start_timestamp"] = epoch_ms(start)
    if end:
        params["end_timestamp"] = epoch_ms(end)
    return _page(await call.json(ep.PERPS_TRADES, params=params))


@operation("Get Perps funding history", toolset=TOOLSET, read_only=True)
async def get_perps_funding_history(
    call: Call, instrument_id: InstrumentId, start: When = None, end: When = None
) -> CursorPage[Raw]:
    """Settled funding rates of an instrument, up to 100 per call. Positive rates mean longs
    pay shorts."""
    params: dict[str, Any] = {"instrument_id": instrument_id}
    if start:
        params["start_timestamp"] = epoch_ms(start)
    if end:
        params["end_timestamp"] = epoch_ms(end)
    return _page(await call.json(ep.PERPS_FUNDING_HISTORY, params=params))


@operation("Get Perps exchange statistics", toolset=TOOLSET, read_only=True)
async def get_perps_exchange_stats(
    call: Call,
    start: Annotated[When, Field(description="Empty means 24 hours ago.")] = None,
    end: When = None,
) -> Raw:
    """Exchange-wide volume, trades, fees and open interest over a window of up to 31 days."""
    end_ms = epoch_ms(end) or now_ms()
    start_ms = epoch_ms(start) or end_ms - 86_400_000
    raw = await call.json(
        ep.PERPS_EXCHANGE_STATS, params={"start_timestamp": start_ms, "end_timestamp": end_ms}
    )
    return Raw.model_validate(perps_slim(raw) if isinstance(raw, dict) else {"data": raw})


class PublicAccount(CompactModel):
    address: str
    registered: bool | None = None
    portfolio: dict[str, Any] | None = None
    builder: dict[str, Any] | None = None
    errors: list[Section] | None = None


@operation("Get a public Perps account", toolset=TOOLSET, read_only=True)
async def get_perps_public_account(
    call: Call,
    address: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$", description="Owner address.")],
    include_builder: Annotated[bool, Field(description="Also the builder profile.")] = False,
) -> PublicAccount:
    """Whether an address has a Perps account, and its public equity and open positions."""
    calls = [
        call.json(ep.PERPS_REGISTERED, params={"address": address}),
        call.json(ep.PERPS_PUBLIC_PORTFOLIO, params={"address": address}),
    ]
    if include_builder:
        calls.append(call.json(ep.PERPS_BUILDER_INFO, params={"address": address}))
    results = await asyncio.gather(*calls, return_exceptions=True)
    names = ("registered", "portfolio", "builder")
    registered = results[0] if not isinstance(results[0], BaseException) else None
    unregistered = isinstance(registered, dict) and registered.get("registered") is False
    errors = [
        Section(section=names[i], error=describe_error(r))
        for i, r in enumerate(results)
        # An address without an account has no portfolio; that is not an error.
        if isinstance(r, BaseException) and not (i == 1 and unregistered)
    ]
    portfolio = results[1] if not isinstance(results[1], BaseException) else None
    builder = results[2] if include_builder and not isinstance(results[2], BaseException) else None
    return PublicAccount(
        address=address,
        registered=(registered or {}).get("registered") if isinstance(registered, dict) else None,
        portfolio=perps_slim(portfolio) if isinstance(portfolio, dict) else None,
        builder=perps_slim(builder) if isinstance(builder, dict) else None,
        errors=errors or None,
    )


@operation("List public Perps fills", toolset=TOOLSET, read_only=True)
async def list_perps_public_fills(
    call: Call,
    address: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")],
    instrument_id: Annotated[
        int | None, Field(description="Required for current_position.")
    ] = None,
    current_position: Annotated[
        bool, Field(description="Only fills of the open position cycle on instrument_id.")
    ] = False,
    start: Annotated[When, Field(description="Empty means 30 days ago.")] = None,
    end: When = None,
    oldest_first: bool = False,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Fill history of a Perps account: prices, sizes, fees and realised PnL, 100 per page."""
    sort = "asc" if oldest_first else "desc"
    if current_position:
        if instrument_id is None:
            raise InvalidRequest("current_position needs instrument_id.")
        params: dict[str, Any] = {"address": address, "instrument_id": instrument_id, "sort": sort}
        if cursor:
            params["cursor"] = cursor
        return _page(await call.json(ep.PERPS_POSITION_FILLS, params=params))
    params = {"address": address, "sort": sort}
    if instrument_id is not None:
        params["instrument_id"] = instrument_id
    if start:
        params["start_timestamp"] = epoch_ms(start)
    if end:
        params["end_timestamp"] = epoch_ms(end)
    if cursor:
        params["cursor"] = cursor
    return _page(await call.json(ep.PERPS_PUBLIC_FILLS, params=params))


@operation("Check a Perps invite code", toolset=TOOLSET, read_only=True)
async def check_perps_invite_code(
    call: Call,
    code: str,
    address: Annotated[
        str | None, Field(description="Would this address be able to use it?")
    ] = None,
) -> Raw:
    """Whether a Perps invite code is valid, optionally for a given address."""
    params = {"code": code} | ({"address": address} if address else {})
    raw = await call.json(ep.PERPS_INVITE_CHECK, params=params)
    return Raw.model_validate(perps_slim(raw) if isinstance(raw, dict) else {"data": raw})
