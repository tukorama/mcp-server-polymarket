import asyncio
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import describe_error
from mcp_server_polymarket.client.paging import epoch_seconds
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Section
from mcp_server_polymarket.models.data import (
    Activity,
    DataRow,
    Position,
    Trade,
    rows,
    slim,
)
from mcp_server_polymarket.ops.registry import Call, Cursor, Wallet, When, operation

TOOLSET = "analytics"
DataLimit = Annotated[int, Field(ge=1, le=200, description="Rows per page.")]
ConditionIds = Annotated[
    list[str] | None, Field(max_length=20, description="Up to 20 condition ids.")
]
EventIds = Annotated[list[str] | None, Field(max_length=20, description="Up to 20 event ids.")]
Window = Literal["day", "week", "month", "all"]


def _join(values: list[str] | None) -> str | None:
    return ",".join(values) if values else None


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v not in (None, "", [])}


def _page(model: type[DataRow], body: Any) -> CursorPage[Any]:
    body = body if isinstance(body, dict) else {}
    pagination = body.get("pagination") or {}
    data = body.get("data")
    return CursorPage(
        items=rows(model, data),
        next_cursor=pagination.get("next_cursor"),
        has_more=pagination.get("has_more"),
    )


def _data(body: Any) -> Any:
    return body.get("data") if isinstance(body, dict) else None


@operation("List positions", toolset=TOOLSET, read_only=True)
async def list_positions(
    call: Call,
    user: Annotated[str | None, Field(description="Wallet whose positions to list.")] = None,
    condition_ids: Annotated[
        ConditionIds, Field(description="Narrow to markets; alone, lists those markets' holders.")
    ] = None,
    status: Literal["OPEN", "REDEEMABLE", "REDEEMABLE_LOST", "MERGEABLE", "CLOSED"] = "OPEN",
    event_ids: EventIds = None,
    title: Annotated[str | None, Field(description="Substring of the market title.")] = None,
    sort_by: Literal[
        "CURRENT_VALUE",
        "PRICE",
        "TOKENS",
        "UNREALIZED_PNL",
        "REALIZED_PNL",
        "TOTAL_PNL",
        "TIMESTAMP",
    ] = "CURRENT_VALUE",
    ascending: bool = False,
    min_value_usd: Annotated[
        float | None, Field(description="Skip positions worth less than this.")
    ] = None,
    include_archived: bool = False,
    limit: DataLimit = 50,
    cursor: Cursor = None,
) -> CursorPage[Position]:
    """A wallet's positions through their lifecycle: OPEN, REDEEMABLE (won, claimable),
    REDEEMABLE_LOST, MERGEABLE (both sides held) or CLOSED, with size, average price, value
    and PnL. Sizes are shares, `_usdc` fields USD. Keep the same filters when paging."""
    if not user and not condition_ids:
        raise InvalidRequest("Give user, condition_ids, or both.")
    body = await call.json(
        ep.DATA_POSITIONS,
        params=_clean(
            {
                "user": user,
                "condition": _join(condition_ids),
                "status": status,
                "event_id": _join(event_ids),
                "title": title,
                "sort_by": sort_by,
                "sort_direction": "ASC" if ascending else "DESC",
                "filter_type": "CASH" if min_value_usd is not None else None,
                "filter_amount": min_value_usd,
                "include_archived": include_archived or None,
                "limit": None if cursor else limit,
                "cursor": cursor,
            }
        ),
    )
    return _page(Position, body)


@operation("List combo positions", toolset=TOOLSET, read_only=True)
async def list_combo_positions(
    call: Call,
    user: Wallet,
    status: Annotated[
        Literal[
            "OPEN", "REDEEMABLE", "PARTIAL", "RESOLVED_WIN", "RESOLVED_LOSS", "RESOLVED_PARTIAL"
        ]
        | None,
        Field(description="Empty lists what the wallet still holds."),
    ] = None,
    condition_ids: ConditionIds = None,
    sort_by: Literal["FIRST_ENTRY", "ENTRY_COST", "CURRENT_VALUE"] | None = None,
    ascending: bool = False,
    limit: DataLimit = 50,
    cursor: Cursor = None,
) -> CursorPage[DataRow]:
    """A wallet's Combo (multi-leg) positions with their legs, cost and value."""
    body = await call.json(
        ep.DATA_COMBO_POSITIONS,
        params=_clean(
            {
                "user": user,
                "status": status,
                "condition": _join(condition_ids),
                "sort_by": sort_by,
                "sort_direction": "ASC" if ascending else "DESC",
                "limit": None if cursor else limit,
                "cursor": cursor,
            }
        ),
    )
    return _page(DataRow, body)


class UserOverview(CompactModel):
    wallet: str
    value_usd: float | None = None
    stats: dict[str, Any] | None = None
    volume: dict[str, Any] | None = None
    errors: list[Section] | None = None


@operation("Get a user overview", toolset=TOOLSET, read_only=True)
async def get_user_overview(
    call: Call,
    user: Wallet,
    volume_since: Annotated[When, Field(description="Start of the volume window.")] = None,
    volume_until: When = None,
) -> UserOverview:
    """A wallet at a glance: portfolio value, profile stats (trades, biggest win, all-time PnL
    breakdown, join date) and trading volume. Use get_user_pnl for the PnL series."""
    results = await asyncio.gather(
        call.json(ep.DATA_VALUE, params={"user": user}),
        call.json(ep.DATA_USER_STATS, params={"user": user}),
        call.json(
            ep.DATA_USER_VOLUME,
            params=_clean(
                {
                    "user": user,
                    "start": epoch_seconds(volume_since),
                    "end": epoch_seconds(volume_until),
                }
            ),
        ),
        return_exceptions=True,
    )
    errors = [
        Section(section=name, error=describe_error(result))
        for name, result in zip(("value", "stats", "volume"), results, strict=True)
        if isinstance(result, BaseException)
    ]
    value, stats, volume = (None if isinstance(r, BaseException) else _data(r) for r in results)
    return UserOverview(
        wallet=user,
        value_usd=(value or {}).get("value") if isinstance(value, dict) else None,
        stats=slim(stats) if isinstance(stats, dict) else None,
        volume=slim(volume) if isinstance(volume, dict) else None,
        errors=errors or None,
    )


class PnlPoint(CompactModel):
    time: str
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    economic_pnl: float | None = None
    fees_paid: float | None = None
    volume_usdc: float | None = None


class UserPnl(CompactModel):
    wallet: str
    interval: str | None = None
    fidelity: str | None = None
    points: list[PnlPoint]
    latest: dict[str, Any] | None = None
    change_economic_pnl: float | None = None


@operation("Get a user's PnL series", toolset=TOOLSET, read_only=True)
async def get_user_pnl(
    call: Call,
    user: Wallet,
    interval: Literal["6h", "12h", "1d", "1w", "1m", "all"] = "1m",
    fidelity: Literal["1h", "3h", "12h", "18h", "1d"] = "1d",
    max_points: Annotated[int, Field(ge=2, le=500, description="Downsample to this many.")] = 60,
) -> UserPnl:
    """A wallet's cumulative PnL over time, with the full breakdown (rebates, rewards, fees) of
    the latest point. economic_pnl is the all-in figure."""
    body = await call.json(
        ep.DATA_USER_PNL, params={"user": user, "interval": interval, "fidelity": fidelity}
    )
    data = _data(body) or {}
    raw = [p for p in data.get("points") or [] if isinstance(p, dict)]
    step = max(1, -(-len(raw) // max_points))
    sampled = raw[::step]
    if raw and sampled[-1] is not raw[-1]:
        sampled.append(raw[-1])
    points = [
        PnlPoint(
            time=slim({"timestamp": p.get("timestamp")})["timestamp"],
            realized_pnl=p.get("realized_pnl"),
            unrealized_pnl=p.get("unrealized_pnl"),
            economic_pnl=p.get("economic_pnl"),
            fees_paid=p.get("fees_paid"),
            volume_usdc=p.get("volume_usdc"),
        )
        for p in sampled
        if p.get("timestamp") is not None
    ]
    first, last = (raw[0], raw[-1]) if raw else ({}, {})
    change = None
    if first.get("economic_pnl") is not None and last.get("economic_pnl") is not None:
        change = round(last["economic_pnl"] - first["economic_pnl"], 6)
    return UserPnl(
        wallet=user,
        interval=data.get("interval"),
        fidelity=data.get("fidelity"),
        points=points,
        latest=slim(last) if last else None,
        change_economic_pnl=change,
    )


@operation("List trades", toolset=TOOLSET, read_only=True)
async def list_trades(
    call: Call,
    user: Annotated[str | None, Field(description="A wallet; empty is the global feed.")] = None,
    condition_ids: ConditionIds = None,
    event_ids: EventIds = None,
    side: Literal["BUY", "SELL"] | None = None,
    min_usd: Annotated[float | None, Field(description="Only trades at least this big.")] = None,
    include_maker_side: Annotated[
        bool, Field(description="Also the maker row of each fill.")
    ] = False,
    start: When = None,
    end: When = None,
    limit: DataLimit = 50,
    cursor: Cursor = None,
) -> CursorPage[Trade]:
    """Trades for a wallet, markets, events, or the whole exchange, newest first. Re-send the
    same filters with each cursor."""
    body = await call.json(
        ep.DATA_TRADES,
        params=_clean(
            {
                "user": user,
                "condition": _join(condition_ids),
                "event_id": _join(event_ids),
                "side": side,
                "filter_type": "CASH" if min_usd is not None else None,
                "filter_amount": min_usd,
                "taker_only": False if include_maker_side else None,
                "start": epoch_seconds(start),
                "end": epoch_seconds(end),
                "limit": limit,
                "cursor": cursor,
            }
        ),
    )
    return _page(Trade, body)


ActivityType = Literal[
    "TRADE",
    "SPLIT",
    "MERGE",
    "REDEEM",
    "CONVERSION",
    "REWARD",
    "MAKER_REBATE",
    "TAKER_REBATE",
    "DEPOSIT",
    "WITHDRAWAL",
]


@operation("List account activity", toolset=TOOLSET, read_only=True)
async def list_activity(
    call: Call,
    user: Wallet,
    types: Annotated[list[ActivityType] | None, Field(description="Empty means all.")] = None,
    condition_ids: ConditionIds = None,
    event_ids: EventIds = None,
    side: Literal["BUY", "SELL"] | None = None,
    start: When = None,
    end: When = None,
    oldest_first: bool = False,
    limit: DataLimit = 50,
    cursor: Cursor = None,
) -> CursorPage[Activity]:
    """A wallet's on-chain activity: trades, splits, merges, redemptions, rewards and rebates,
    optionally deposits and withdrawals. Re-send the same filters with each cursor."""
    wants_transfers = bool(types and {"DEPOSIT", "WITHDRAWAL"} & set(types))
    body = await call.json(
        ep.DATA_ACTIVITY,
        params=_clean(
            {
                "user": user,
                "type": _join(list(types) if types else None),
                "condition": _join(condition_ids),
                "event_id": _join(event_ids),
                "side": side,
                "start": epoch_seconds(start),
                "end": epoch_seconds(end),
                "sort_direction": "ASC" if oldest_first else "DESC",
                "exclude_deposits_withdrawals": False if wants_transfers else None,
                "limit": limit,
                "cursor": cursor,
            }
        ),
    )
    return _page(Activity, body)


@operation("List combo activity", toolset=TOOLSET, read_only=True)
async def list_combo_activity(
    call: Call,
    user: Wallet,
    condition_ids: ConditionIds = None,
    limit: DataLimit = 50,
    cursor: Cursor = None,
) -> CursorPage[DataRow]:
    """A wallet's Combo lifecycle and redemptions, newest first."""
    body = await call.json(
        ep.DATA_COMBO_ACTIVITY,
        params=_clean(
            {"user": user, "condition": _join(condition_ids), "limit": limit, "cursor": cursor}
        ),
    )
    return _page(DataRow, body)


class Holders(CompactModel):
    token_id: str | None = None
    holders: list[DataRow]


class HoldersPage(CompactModel):
    tokens: list[Holders]
    next_cursor: str | None = None


@operation("List a market's top holders", toolset=TOOLSET, read_only=True)
async def list_market_holders(
    call: Call,
    condition_id: Annotated[str, Field(description="The market's condition id.")],
    min_shares: Annotated[float | None, Field(description="Minimum holding.")] = None,
    include_pnl: Annotated[
        bool, Field(description="Add each holder's entry price and PnL.")
    ] = False,
    limit: Annotated[int, Field(ge=1, le=100, description="Holders per outcome.")] = 20,
    cursor: Cursor = None,
) -> HoldersPage:
    """The largest holders of each outcome of a market, netted per wallet."""
    body = await call.json(
        ep.DATA_HOLDERS,
        params=_clean(
            {
                "condition": condition_id,
                "min_balance": min_shares,
                "include_pnl": include_pnl or None,
                "limit": None if cursor else limit,
                "cursor": cursor,
            }
        ),
    )
    data = _data(body) or []
    pagination = (body or {}).get("pagination") or {} if isinstance(body, dict) else {}
    return HoldersPage(
        tokens=[
            Holders(token_id=entry.get("token_id"), holders=rows(DataRow, entry.get("holders")))
            for entry in data
            if isinstance(entry, dict)
        ],
        next_cursor=pagination.get("next_cursor"),
    )


class OpenInterest(CompactModel):
    markets: list[DataRow]
    total: float | None = None


@operation("Get open interest", toolset=TOOLSET, read_only=True)
async def get_open_interest(
    call: Call, condition_ids: Annotated[list[str], Field(min_length=1, max_length=20)]
) -> OpenInterest:
    """Open interest in USD for up to 20 markets: the money currently behind their prices."""
    body = await call.json(ep.DATA_OPEN_INTEREST, params={"condition": _join(condition_ids)})
    markets = rows(DataRow, _data(body))
    total = sum(float(getattr(m, "value", 0) or 0) for m in markets)
    return OpenInterest(markets=markets, total=total if markets else None)


@operation("Get an event's live volume", toolset=TOOLSET, read_only=True)
async def get_event_live_volume(call: Call, event_id: str) -> DataRow:
    """Cumulative taker volume in shares for each market of an event, and the total."""
    body = await call.json(ep.DATA_LIVE_VOLUME, params={"event_id": event_id})
    data = _data(body)
    return DataRow.model_validate(slim(data) if isinstance(data, dict) else {})


@operation("Get resolution state", toolset=TOOLSET, read_only=True)
async def get_resolutions(
    call: Call,
    condition_ids: ConditionIds = None,
    question_ids: Annotated[list[str] | None, Field(max_length=20)] = None,
    event_id: str | None = None,
) -> CursorPage[DataRow]:
    """Where each market is in resolution: proposed price, disputes, final outcome and the
    expected settlement time. Give exactly one of condition_ids, question_ids or event_id."""
    given = [x for x in (condition_ids, question_ids, event_id) if x]
    if len(given) != 1:
        raise InvalidRequest("Give exactly one of condition_ids, question_ids or event_id.")
    body = await call.json(
        ep.DATA_RESOLUTIONS,
        params=_clean(
            {
                "condition": _join(condition_ids),
                "question_id": _join(question_ids),
                "event_id": event_id,
            }
        ),
    )
    return _page(DataRow, body)


@operation("Look up tokens", toolset=TOOLSET, read_only=True)
async def lookup_tokens(
    call: Call,
    token_ids: Annotated[list[str] | None, Field(max_length=50)] = None,
    condition_ids: Annotated[list[str] | None, Field(max_length=10)] = None,
) -> CursorPage[DataRow]:
    """Resolve token ids, or every token of some markets, to outcome, opposite token, market,
    event, neg-risk structure and settlement. Give token_ids or condition_ids."""
    if bool(token_ids) == bool(condition_ids):
        raise InvalidRequest("Give exactly one of token_ids or condition_ids.")
    body = await call.json(
        ep.DATA_TOKENS,
        params=_clean({"token_id": _join(token_ids), "condition": _join(condition_ids)}),
    )
    return _page(DataRow, body)


@operation("Get the trader leaderboard", toolset=TOOLSET, read_only=True)
async def get_leaderboard(
    call: Call,
    window: Window = "day",
    sort_by: Literal["PNL", "VOLUME"] = "PNL",
    category: Annotated[
        str, Field(description="overall, a market category such as sports, or combos.")
    ] = "overall",
    user: Annotated[
        str | None, Field(description="One wallet's ranks instead of the board.")
    ] = None,
    limit: DataLimit = 20,
    cursor: Cursor = None,
) -> CursorPage[DataRow]:
    """Top traders by realised PnL or volume over a window, or one wallet's standing."""
    body = await call.json(
        ep.DATA_LEADERBOARD,
        params=_clean(
            {
                "time_period": window,
                "sort_by": sort_by,
                "category": category,
                "user": user,
                "limit": None if (cursor or user) else limit,
                "cursor": cursor,
            }
        ),
    )
    if user:
        data = _data(body)
        return CursorPage(
            items=[DataRow.model_validate(slim(data))] if isinstance(data, dict) else []
        )
    return _page(DataRow, body)


@operation("List the biggest wins", toolset=TOOLSET, read_only=True)
async def list_biggest_wins(
    call: Call,
    window: Window = "day",
    category: str = "overall",
    limit: DataLimit = 20,
    cursor: Cursor = None,
) -> CursorPage[DataRow]:
    """The largest single winning positions resolved in a window."""
    body = await call.json(
        ep.DATA_BIGGEST_WINNERS,
        params=_clean(
            {
                "time_period": window,
                "category": category,
                "limit": None if cursor else limit,
                "cursor": cursor,
            }
        ),
    )
    return _page(DataRow, body)


@operation("Get wallet approvals", toolset=TOOLSET, read_only=True)
async def get_wallet_approvals(call: Call, user: Wallet) -> DataRow:
    """Which Polymarket contracts a wallet has approved to move its pUSD and outcome tokens.
    Missing approvals are why orders fail with 'not enough balance / allowance'."""
    body = await call.json(ep.DATA_APPROVALS, params={"user": user})
    data = _data(body)
    return DataRow.model_validate(slim(data) if isinstance(data, dict) else {})
