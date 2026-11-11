from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.cache import HOUR
from mcp_server_polymarket.client.paging import next_offset, read_offset
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw
from mcp_server_polymarket.ops.registry import Call, Cursor, Limit, operation

TOOLSET = "sports"


class Sport(CompactModel):
    id: str | None = None
    sport: str | None = None
    name: str | None = None
    resolution: str | None = None
    series: str | None = None
    tags: str | None = None
    ordering: str | None = None


class SportsMetadata(CompactModel):
    sports: list[Sport] | None = None
    market_types: list[Any] | None = None
    total: int | None = None


@operation("Get sports metadata", toolset=TOOLSET, read_only=True)
async def get_sports_metadata(
    call: Call,
    kind: Annotated[
        Literal["sports", "market_types"],
        Field(
            description="sports: leagues with their series and tag ids; market_types: the "
            "valid sports_market_types values."
        ),
    ] = "sports",
    search: Annotated[str | None, Field(description="Filter by sport code or name.")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> SportsMetadata:
    """Sports and leagues Polymarket lists, with the series id and tag ids to browse their
    events (list_events series_id), or the market types (moneyline, spreads ...). Cached for an
    hour."""
    cache = call.runtime.cache
    if kind == "market_types":
        raw = cache.get("gamma:market-types", HOUR)
        if raw is None:
            raw = await call.json(ep.GAMMA_SPORTS_MARKET_TYPES)
            cache.set("gamma:market-types", raw)
        types = raw.get("marketTypes", raw) if isinstance(raw, dict) else raw
        return SportsMetadata(market_types=types if isinstance(types, list) else [types])
    rows = cache.get("gamma:sports", HOUR)
    if rows is None:
        rows = await call.json(ep.GAMMA_SPORTS) or []
        cache.set("gamma:sports", rows)
    needle = search.lower() if search else None
    sports = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = f"{row.get('sport', '')} {row.get('name', '')}".lower()
        if needle and needle not in text:
            continue
        sports.append(
            Sport(
                id=str(row.get("id")) if row.get("id") is not None else None,
                sport=row.get("sport"),
                name=row.get("name"),
                resolution=row.get("resolution"),
                series=str(row.get("series")) if row.get("series") is not None else None,
                tags=row.get("tags"),
                ordering=row.get("ordering"),
            )
        )
    return SportsMetadata(sports=sports[:limit], total=len(sports))


class Team(Raw):
    id: int | str | None = None
    name: str | None = None
    league: str | None = None


@operation("List sports teams", toolset=TOOLSET, read_only=True)
async def list_teams(
    call: Call,
    team_id: Annotated[str | None, Field(description="One team by id.")] = None,
    league: Annotated[str | None, Field(description="e.g. nba, nfl, epl.")] = None,
    name: str | None = None,
    abbreviation: str | None = None,
    limit: Limit = 50,
    cursor: Cursor = None,
) -> CursorPage[Team]:
    """Teams with league, record, abbreviation and colours."""
    if team_id:
        raw = await call.json(ep.GAMMA_TEAM, path_args={"id": team_id})
        return CursorPage(items=[Team.model_validate(raw)] if isinstance(raw, dict) else [])
    offset = read_offset(cursor)
    params = {
        "limit": limit,
        "offset": offset,
        "league": league,
        "name": name,
        "abbreviation": abbreviation,
    }
    rows = await call.json(ep.GAMMA_TEAMS, params={k: v for k, v in params.items() if v}) or []
    return CursorPage(
        items=[Team.model_validate(r) for r in rows if isinstance(r, dict)],
        next_cursor=next_offset(offset, len(rows), limit),
    )


@operation("List sports results", toolset=TOOLSET, read_only=True)
async def list_sports_results(
    call: Call, limit: Limit = 20, cursor: Cursor = None
) -> CursorPage[Raw]:
    """Recently settled sports events. Look one up with get_event."""
    offset = read_offset(cursor)
    rows = await call.json(ep.GAMMA_EVENTS_RESULTS, params={"limit": limit, "offset": offset}) or []
    return CursorPage(
        items=[Raw.model_validate(r) for r in rows if isinstance(r, dict)],
        next_cursor=next_offset(offset, len(rows), limit),
    )
