from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw
from mcp_server_polymarket.models.data import slim
from mcp_server_polymarket.ops.registry import Call, Cursor, operation

TOOLSET = "rewards"
Day = Annotated[str | None, Field(description="YYYY-MM-DD, UTC. Empty means today.")]
_END = ("LTE=", "", None)


def _day(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError as exc:
        raise InvalidRequest(f"{value!r} is not a date (YYYY-MM-DD).") from exc


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _page(body: Any) -> CursorPage[Raw]:
    body = body if isinstance(body, dict) else {"data": body}
    data = body.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    cursor = body.get("next_cursor")
    return CursorPage(
        items=[Raw.model_validate(slim(r)) for r in rows if isinstance(r, dict)],
        next_cursor=None if cursor in _END else cursor,
        count=body.get("count"),
    )


def _who(call: Call) -> dict[str, Any]:
    identity = call.identity
    return {"signature_type": identity.signature_type, "maker_address": identity.wallet_address}


@operation("List markets paying liquidity rewards", toolset=TOOLSET, read_only=True)
async def list_reward_markets(
    call: Call,
    condition_id: Annotated[
        str | None, Field(description="Present and future reward configs of one market.")
    ] = None,
    query: Annotated[str | None, Field(description="Search market questions.")] = None,
    tag_slugs: list[str] | None = None,
    event_ids: list[str] | None = None,
    order_by: Literal[
        "rate_per_day",
        "volume_24hr",
        "spread",
        "max_spread",
        "min_size",
        "competitiveness",
        "end_date",
        "created_at",
    ] = "rate_per_day",
    ascending: bool = False,
    max_spread: Annotated[float | None, Field(description="Current spread at most this.")] = None,
    min_volume_24h: float | None = None,
    sponsored: Annotated[bool, Field(description="Sponsored programs instead of native.")] = False,
    all_configs: Annotated[
        bool, Field(description="Every active configuration, unfiltered, in raw form.")
    ] = False,
    page_size: Annotated[int, Field(ge=1, le=500)] = 50,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Markets with liquidity rewards: daily rate, minimum size and maximum spread to earn, and
    current prices. Quote within max spread with at least min size to score."""
    if condition_id:
        body = await call.json(
            ep.CLOB_REWARDS_MARKET,
            path_args={"condition_id": condition_id},
            params=_clean({"sponsored": sponsored or None, "next_cursor": cursor}),
        )
    elif all_configs:
        body = await call.json(
            ep.CLOB_REWARDS_CURRENT,
            params=_clean({"sponsored": sponsored or None, "next_cursor": cursor}),
        )
    else:
        body = await call.json(
            ep.CLOB_REWARDS_MULTI,
            params=_clean(
                {
                    "q": query,
                    "tag_slug": tag_slugs,
                    "event_id": event_ids,
                    "order_by": order_by,
                    "position": "ASC" if ascending else "DESC",
                    "max_spread": max_spread,
                    "min_volume_24hr": min_volume_24h,
                    "page_size": page_size,
                    "next_cursor": cursor,
                }
            ),
        )
    return _page(body)


@operation("Get a maker's rebates", toolset=TOOLSET, read_only=True)
async def get_maker_rebates(
    call: Call,
    maker_address: Annotated[
        str | None, Field(description="Empty uses the configured wallet.")
    ] = None,
    date: Day = None,
) -> CursorPage[Raw]:
    """Fees rebated to a maker on one day, per market."""
    address = maker_address or call.identity.wallet_address
    if not address:
        raise InvalidRequest("Give maker_address, or configure POLYMARKET_WALLET_ADDRESS.")
    body = await call.json(ep.CLOB_REBATES, params={"date": _day(date), "maker_address": address})
    return _page(body)


class RewardEarnings(CompactModel):
    date: str | None = None
    total: list[Raw] | None = None
    by_market: list[Raw] | None = None
    percentages: dict[str, Any] | None = None
    next_cursor: str | None = None


@operation(
    "Get my liquidity reward earnings",
    toolset=TOOLSET,
    requires=("clob_l2",),
    read_only=True,
)
async def get_my_reward_earnings(
    call: Call,
    view: Annotated[
        Literal["total", "by_market", "live_percentages"],
        Field(description="live_percentages: your current share of each market's rewards."),
    ] = "total",
    date: Day = None,
    sponsored: bool = False,
    cursor: Cursor = None,
) -> RewardEarnings:
    """Your liquidity reward earnings for a day, in total or per market, or your live share of
    each market's rewards right now."""
    who = _who(call)
    if view == "live_percentages":
        body = await call.json(ep.CLOB_USER_REWARD_PERCENTAGES, params=_clean(who))
        return RewardEarnings(percentages=body if isinstance(body, dict) else None)
    day = _day(date)
    params = _clean({**who, "date": day, "sponsored": sponsored or None})
    if view == "total":
        body = await call.json(ep.CLOB_USER_EARNINGS_TOTAL, params=params)
        rows = body if isinstance(body, list) else []
        return RewardEarnings(
            date=day, total=[Raw.model_validate(r) for r in rows if isinstance(r, dict)]
        )
    page = _page(await call.json(ep.CLOB_USER_EARNINGS, params={**params, "next_cursor": cursor}))
    return RewardEarnings(date=day, by_market=page.items, next_cursor=page.next_cursor)


@operation(
    "List my reward markets",
    toolset=TOOLSET,
    requires=("clob_l2",),
    read_only=True,
)
async def list_my_reward_markets(
    call: Call,
    date: Day = None,
    query: str | None = None,
    tag_slugs: list[str] | None = None,
    only_open_orders: bool = False,
    only_open_positions: bool = False,
    only_mergeable: bool = False,
    favorites_only: bool = False,
    no_competition: bool = False,
    order_by: Literal[
        "earnings",
        "earning_percentage",
        "rate_per_day",
        "max_spread",
        "min_size",
        "spread",
        "volume_24hr",
        "end_date",
        "competitiveness",
    ] = "earnings",
    ascending: bool = False,
    page_size: Annotated[int, Field(ge=1, le=500)] = 50,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Reward markets with your earnings and live share in each, for planning where to quote."""
    body = await call.json(
        ep.CLOB_USER_REWARD_MARKETS,
        params=_clean(
            {
                **_who(call),
                "date": _day(date) if date else None,
                "q": query,
                "tag_slug": tag_slugs,
                "only_open_orders": only_open_orders or None,
                "only_open_positions": only_open_positions or None,
                "only_mergeable": only_mergeable or None,
                "favorite_markets": favorites_only or None,
                "no_competition": no_competition or None,
                "order_by": order_by,
                "position": "ASC" if ascending else "DESC",
                "page_size": page_size,
                "next_cursor": cursor,
            }
        ),
    )
    return _page(body)


class Scoring(CompactModel):
    scoring: dict[str, bool]


@operation(
    "Check whether my orders score rewards",
    toolset=TOOLSET,
    requires=("clob_l2",),
    read_only=True,
)
async def get_my_order_scoring(
    call: Call, order_ids: Annotated[list[str], Field(min_length=1, max_length=100)]
) -> Scoring:
    """Whether each of your resting orders currently qualifies for liquidity rewards."""
    if len(order_ids) == 1:
        body = await call.json(ep.CLOB_ORDER_SCORING, params={"order_id": order_ids[0]})
        return Scoring(scoring={order_ids[0]: bool((body or {}).get("scoring"))})
    body = await call.json(ep.CLOB_ORDERS_SCORING, body=order_ids)
    return Scoring(scoring={k: bool(v) for k, v in (body or {}).items()})
