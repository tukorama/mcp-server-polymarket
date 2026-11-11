"""Combos: positions on several outcomes at once, priced by request for quote."""

from typing import Annotated, Any

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw
from mcp_server_polymarket.ops.registry import Call, Cursor, operation

TOOLSET = "combos"


class ComboMarket(CompactModel):
    id: str
    condition_id: str | None = None
    title: str | None = None
    slug: str | None = None
    outcomes: list[dict[str, Any]] = []
    volume: float | None = None
    tags: list[str] | None = None
    pending: bool | None = None


@operation("List markets usable in Combos", toolset=TOOLSET, read_only=True)
async def list_combo_markets(
    call: Call,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    exclude: Annotated[list[str] | None, Field(description="Market ids to leave out.")] = None,
    cursor: Cursor = None,
) -> CursorPage[ComboMarket]:
    """Markets that can be legs of a Combo, most traded first, with the position id of each
    outcome (the leg ids trade_combo takes)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if exclude:
        params["exclude"] = ",".join(exclude)
    body = await call.json(ep.COMBO_MARKETS, params=params) or {}
    markets = []
    for row in body.get("markets") or []:
        names = row.get("outcomes") or []
        prices = row.get("outcome_prices") or []
        positions = row.get("position_ids") or []
        markets.append(
            ComboMarket(
                id=str(row.get("id")),
                condition_id=row.get("condition_id"),
                title=row.get("title"),
                slug=row.get("slug"),
                outcomes=[
                    {
                        "name": name,
                        "price": float(prices[i]) if i < len(prices) else None,
                        "position_id": positions[i] if i < len(positions) else None,
                    }
                    for i, name in enumerate(names)
                ],
                volume=row.get("volume"),
                tags=row.get("tags") or None,
                pending=row.get("pending") or None,
            )
        )
    return CursorPage(items=markets, next_cursor=body.get("next_cursor") or None)


@operation(
    "Get a Combo request",
    toolset=TOOLSET,
    requires=("clob_l2",),
    read_only=True,
)
async def get_combo_request(call: Call, request_id: str) -> Raw:
    """The state of a Combo request: its quote, and whether it was FILLED, CONFIRMED, FAILED,
    EXPIRED or CANCELED."""
    body = await call.json(ep.COMBO_REQUEST, path_args={"id": request_id})
    return Raw.model_validate(body if isinstance(body, dict) else {"request": body})


class CollateralReturnPlan(CompactModel):
    """A plan to release pUSD locked in Combo positions before they resolve."""

    model_config = {"extra": "allow"}

    plan_hash: str | None = None
    operations: list[Any] | None = None
    truncated: bool | None = None


@operation("Plan a Combo collateral return", toolset=TOOLSET, read_only=True)
async def plan_collateral_return(call: Call) -> CollateralReturnPlan:
    """What pUSD the configured wallet could release now from compatible Combo positions, and
    the plan_hash to execute it with execute_collateral_return. Nothing is moved."""
    wallet = call.identity.wallet_address
    if not wallet:
        raise InvalidRequest("No wallet is configured.", hint="Set POLYMARKET_WALLET_ADDRESS.")
    body = await call.json(ep.COLLATERAL_RETURN_PLAN, body={"wallet": wallet}) or {}
    body = dict(body) if isinstance(body, dict) else {}
    # The router call is what would be signed; the agent needs the summary, not calldata.
    body.pop("router_call", None)
    return CollateralReturnPlan.model_validate(body)
