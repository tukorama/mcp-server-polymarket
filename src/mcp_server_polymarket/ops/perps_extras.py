from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.paging import epoch_ms
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, Raw, WriteResult
from mcp_server_polymarket.ops.perps_market import perps_slim
from mcp_server_polymarket.ops.registry import Call, Cursor, When, operation

TOOLSET = "perps-extras"
Address = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]


def _raw(body: Any) -> Raw:
    return Raw.model_validate(perps_slim(body) if isinstance(body, dict) else {"data": body})


@operation("Get the Perps leaderboard", toolset=TOOLSET, read_only=True)
async def get_perps_leaderboard(
    call: Call,
    window: Literal["day", "week", "month", "all"] = "week",
    sort_by: Literal["pnl", "notional", "account_value", "roi"] = "pnl",
    address: Annotated[str | None, Field(description="Also this account's row.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> Raw:
    """Top Perps traders by PnL, notional volume, account value or ROI."""
    params: dict[str, Any] = {
        "window": window,
        "sort_by": sort_by,
        "limit": limit,
        "offset": offset,
    }
    if address:
        params["address"] = address
    return _raw(await call.json(ep.PERPS_LEADERBOARD, params=params))


@operation("List Perps trading competitions", toolset=TOOLSET, read_only=True)
async def list_perps_competitions(
    call: Call,
    address: Annotated[str | None, Field(description="Show this account's progress.")] = None,
) -> Raw:
    """Perps trading competitions, newest first, with the live one, and with an address its
    current rank and progress toward qualifying."""
    params = {"address": address} if address else None
    competitions = await call.json(ep.PERPS_COMPETITIONS, params=params)
    widget = await call.json(ep.PERPS_COMPETITION_WIDGET, params=params)
    data = perps_slim(competitions) if isinstance(competitions, dict) else {"data": competitions}
    data["live_summary"] = perps_slim(widget) if isinstance(widget, dict) else widget
    return Raw.model_validate(data)


class Competition(CompactModel):
    competition: dict[str, Any] | None = None
    leaderboard: dict[str, Any] | None = None
    account: dict[str, Any] | None = None


@operation("Get a Perps trading competition", toolset=TOOLSET, read_only=True)
async def get_perps_competition(
    call: Call,
    competition_id: int,
    address: Annotated[str | None, Field(description="Also this account's standing.")] = None,
    leaderboard_limit: Annotated[int, Field(ge=0, le=200)] = 20,
    include_unqualified: bool = False,
) -> Competition:
    """One competition's rules, boosted markets and prizes, its leaderboard, and optionally an
    account's rank, score and positions."""
    result = Competition()
    body = await call.json(ep.PERPS_COMPETITION, params={"competition_id": competition_id})
    result.competition = perps_slim(body) if isinstance(body, dict) else None
    if leaderboard_limit:
        board = await call.json(
            ep.PERPS_COMPETITION_LEADERBOARD,
            params={
                "competition_id": competition_id,
                "limit": leaderboard_limit,
                "include_unqualified": include_unqualified,
            },
        )
        result.leaderboard = perps_slim(board) if isinstance(board, dict) else {"rows": board}
    if address:
        account = await call.json(
            ep.PERPS_COMPETITION_ACCOUNT,
            params={"competition_id": competition_id, "address": address},
        )
        result.account = perps_slim(account) if isinstance(account, dict) else None
    return result


@operation("Get Perps position share cards", toolset=TOOLSET, read_only=True)
async def get_perps_position_snapshots(
    call: Call,
    address: Address,
    instrument_ids: Annotated[
        list[int], Field(max_length=20, description="Open positions to card.")
    ] = [],  # noqa: B006
    fills: Annotated[
        list[dict[str, Any]],
        Field(
            max_length=4,
            description="Closed trades to card: {instrument_id, trade_id, timestamp(ms)}.",
        ),
    ] = [],  # noqa: B006
) -> Raw:
    """Numbers, candles and markers for sharing positions or past trades of an account."""
    if not instrument_ids and not fills:
        raise InvalidRequest("Give instrument_ids, fills, or both.")
    body = await call.json(
        ep.PERPS_POSITION_SNAPSHOTS,
        body={
            "address": address,
            "active_instrument_ids": instrument_ids,
            "history_fills": fills,
        },
    )
    return _raw(body)


class Blp(CompactModel):
    enrollment: Any = None
    liquidations: list[Raw] | None = None
    next_cursor: str | None = None


@operation(
    "Get my backstop liquidity provider status",
    toolset=TOOLSET,
    requires=("perps_session",),
    read_only=True,
)
async def get_my_perps_blp(
    call: Call,
    include_liquidations: bool = True,
    start: When = None,
    end: When = None,
    cursor: Cursor = None,
) -> Blp:
    """For backstop liquidity providers: which instruments the account is enrolled in, and the
    liquidations it took over."""
    enrollment = await call.json(ep.PERPS_BLP_ENROLLMENT)
    result = Blp(enrollment=perps_slim(enrollment))
    if include_liquidations:
        params = {
            "start_timestamp": epoch_ms(start),
            "end_timestamp": epoch_ms(end),
            "cursor": cursor,
        }
        body = await call.json(
            ep.PERPS_BLP_LIQUIDATIONS, params={k: v for k, v in params.items() if v}
        )
        body = body if isinstance(body, dict) else {"data": body}
        result.liquidations = [Raw.model_validate(perps_slim(r)) for r in body.get("data") or []]
        result.next_cursor = body.get("cursor") or None
    return result


async def _describe_blp(call: Call, instrument_id: int, active: bool) -> str:
    action = "Subscribe to" if active else "Stop"
    return (
        f"{action} backstop liquidations on instrument {instrument_id}? Subscribing sets 3x "
        "leverage there and locks it."
    )


@operation(
    "Set backstop liquidity provision",
    toolset=TOOLSET,
    requires=("perps_session",),
    gate="trading",
    describe=_describe_blp,
)
async def set_perps_blp_enrollment(call: Call, instrument_id: int, active: bool) -> WriteResult:
    """For enrolled backstop liquidity providers: take over liquidations on an instrument, or
    stop. Subscribing fixes leverage there at 3x."""
    from mcp_server_polymarket.ops.perps_trading import _rejected

    data = await call.json(
        ep.PERPS_BLP_ENROLL, body={"instrument_id": instrument_id, "active": active}
    )
    if _rejected(data):
        return WriteResult(status="rejected", message=str(data.get("error")), raw=data)
    return WriteResult(status="done", raw=perps_slim(data) if isinstance(data, dict) else data)
