from typing import Annotated, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.paging import epoch_seconds
from mcp_server_polymarket.errors import InvalidRequest, NotFound
from mcp_server_polymarket.models.base import CompactModel, CursorPage
from mcp_server_polymarket.models.clob import (
    LiveActivity,
    OrderBook,
    OrderBookMarket,
    Quote,
    TradingInfo,
)
from mcp_server_polymarket.models.data import slim
from mcp_server_polymarket.ops.registry import Call, Cursor, TokenId, When, operation

TOOLSET = "market-data"
TokenIds = Annotated[list[str], Field(min_length=1, max_length=50, description="Token ids.")]
_END = ("LTE=", "")


async def fetch_books(call: Call, token_ids: list[str]) -> list[OrderBook]:
    """Order books for tokens; one request whatever the count."""
    if len(token_ids) == 1:
        raw = await call.json(ep.CLOB_BOOK, params={"token_id": token_ids[0]})
        rows = [raw] if isinstance(raw, dict) else []
    else:
        rows = await call.json(ep.CLOB_BOOKS, body=[{"token_id": t} for t in token_ids]) or []
    books = [
        OrderBook.from_api(row) for row in rows if isinstance(row, dict) and row.get("asset_id")
    ]
    if not books:
        raise NotFound(
            "No order book for these tokens.",
            hint="Check the token ids with get_market; closed markets have no book.",
        )
    return books


class OrderBooks(CompactModel):
    books: list[OrderBook]


@operation("Get order books", toolset=TOOLSET, read_only=True)
async def get_order_books(
    call: Call,
    token_ids: TokenIds,
    depth: Annotated[int, Field(ge=1, le=200, description="Levels per side.")] = 10,
) -> OrderBooks:
    """Order books for outcome tokens, best prices first on each side, with tick size and
    minimum order size. Prices are probabilities (0-1) in pUSD; sizes are shares."""
    books = await fetch_books(call, token_ids)
    for book in books:
        book.bids, book.asks = book.bids[:depth], book.asks[:depth]
    return OrderBooks(books=books)


class Quotes(CompactModel):
    quotes: list[Quote]


@operation("Get quotes", toolset=TOOLSET, read_only=True)
async def get_quotes(call: Call, token_ids: TokenIds) -> Quotes:
    """Best bid and ask, midpoint, spread, top sizes and last trade for each token, in one
    call. Use get_order_books for depth."""
    books = await fetch_books(call, token_ids)
    return Quotes(quotes=[Quote.from_book(book) for book in books])


class PricePoint(CompactModel):
    time: str
    price: float


class PriceHistory(CompactModel):
    token_id: str
    resolution_seconds: int | None = None
    points: list[PricePoint]
    next_cursor: str | None = None
    first: float | None = None
    last: float | None = None
    change: float | None = None
    high: float | None = None
    low: float | None = None


@operation("Get price history", toolset=TOOLSET, read_only=True)
async def get_price_history(
    call: Call,
    token_id: TokenId,
    interval: Annotated[
        Literal["1h", "6h", "1d", "1w", "1m", "max"] | None,
        Field(description="Window ending now. Leave empty when giving start."),
    ] = "1w",
    start: When = None,
    end: When = None,
    bucket_seconds: Annotated[
        int | None,
        Field(ge=60, le=86400, description="Point spacing. Empty lets the server pick."),
    ] = None,
    as_of: Annotated[When, Field(description="Price at one moment instead of a series.")] = None,
    limit: Annotated[int, Field(ge=1, le=2000, description="Most points to return.")] = 500,
    cursor: Cursor = None,
) -> PriceHistory:
    """An outcome token's price (probability) over time, with the window's first, last, high
    and low. For multi-day windows prefer bucket_seconds of 300, 1800, 10800 or 43200."""
    params: dict[str, object] = {"token_id": token_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    elif as_of:
        params["as_of"] = epoch_seconds(as_of)
    elif start:
        params["start"] = epoch_seconds(start)
        if end:
            params["end"] = epoch_seconds(end)
    elif interval:
        params["interval"] = interval
    else:
        raise InvalidRequest("Give interval, start (optionally with end), or as_of.")
    if bucket_seconds and not as_of:
        params["bucket_seconds"] = bucket_seconds
    body = await call.json(ep.DATA_PRICES_HISTORY, params=params) or {}
    data = body.get("data") if isinstance(body, dict) else None
    raw_points = data if isinstance(data, list) else []
    points = []
    for point in raw_points:
        if isinstance(point, dict):
            t, p = point.get("t", point.get("timestamp")), point.get("p", point.get("price"))
        elif isinstance(point, list | tuple) and len(point) >= 2:
            t, p = point[0], point[1]
        else:
            continue
        if t is None or p is None:
            continue
        points.append(PricePoint(time=slim({"timestamp": t})["timestamp"], price=float(p)))
    prices = [p.price for p in points]
    pagination = body.get("pagination") or {} if isinstance(body, dict) else {}
    return PriceHistory(
        token_id=token_id,
        resolution_seconds=raw_points[0].get("resolution_seconds")
        if raw_points and isinstance(raw_points[0], dict)
        else None,
        points=points,
        next_cursor=pagination.get("next_cursor"),
        first=prices[0] if prices else None,
        last=prices[-1] if prices else None,
        change=round(prices[-1] - prices[0], 6) if len(prices) > 1 else None,
        high=max(prices) if prices else None,
        low=min(prices) if prices else None,
    )


@operation("Get market trading rules", toolset=TOOLSET, read_only=True)
async def get_market_trading_info(
    call: Call,
    condition_id: Annotated[str | None, Field(description="0x condition id.")] = None,
    token_id: Annotated[str | None, Field(description="Any outcome token of the market.")] = None,
) -> TradingInfo:
    """The CLOB's rules for one market: tokens and outcomes, tick size, minimum order, base
    fees, fee curve, rewards and whether it is accepting orders. Give condition_id or token_id."""
    if bool(condition_id) == bool(token_id):
        raise InvalidRequest("Give exactly one of condition_id or token_id.")
    if token_id:
        mapping = await call.json(ep.CLOB_MARKET_BY_TOKEN, path_args={"token_id": token_id})
        condition_id = (mapping or {}).get("condition_id")
        if not condition_id:
            raise NotFound(f"No market for token {token_id}.")
    raw = await call.json(ep.CLOB_MARKET_INFO, path_args={"condition_id": str(condition_id)})
    if not isinstance(raw, dict) or not raw.get("c"):
        raise NotFound(f"No CLOB market {condition_id}.")
    return TradingInfo.from_api(raw)


@operation("List order book markets", toolset=TOOLSET, read_only=True)
async def list_order_book_markets(
    call: Call,
    sampling: Annotated[
        bool, Field(description="Only markets currently paying liquidity rewards.")
    ] = False,
    simplified: Annotated[bool, Field(description="Tokens and prices only, smaller pages.")] = True,
    cursor: Cursor = None,
) -> CursorPage[OrderBookMarket]:
    """The CLOB's own market list in its native paging. Usually list_markets is the better
    browser; this one lists reward-eligible (sampling) markets."""
    if sampling:
        endpoint = ep.CLOB_SAMPLING_SIMPLIFIED_MARKETS if simplified else ep.CLOB_SAMPLING_MARKETS
    else:
        endpoint = ep.CLOB_SIMPLIFIED_MARKETS
        if not simplified:
            raise InvalidRequest("Full rows are only available with sampling=true.")
    body = await call.json(endpoint, params={"next_cursor": cursor} if cursor else None) or {}
    rows = body.get("data") or []
    nxt = body.get("next_cursor")
    return CursorPage(
        items=[OrderBookMarket.from_api(r) for r in rows if isinstance(r, dict)],
        next_cursor=None if nxt in _END else nxt,
        count=body.get("count"),
    )


class LiveActivities(CompactModel):
    markets: list[LiveActivity]


@operation("Get live market activity", toolset=TOOLSET, read_only=True)
async def get_live_activity(
    call: Call,
    condition_ids: Annotated[list[str], Field(min_length=1, max_length=50)],
) -> LiveActivities:
    """Which of these markets are live on the site, with their market, event and series slugs."""
    if len(condition_ids) == 1:
        raw = await call.json(
            ep.CLOB_LIVE_ACTIVITY_ONE, path_args={"condition_id": condition_ids[0]}
        )
        rows = [raw] if isinstance(raw, dict) else []
    else:
        rows = await call.json(ep.CLOB_LIVE_ACTIVITY, body=condition_ids) or []
    return LiveActivities(
        markets=[
            LiveActivity.from_api(r) for r in rows if isinstance(r, dict) and r.get("condition_id")
        ]
    )
