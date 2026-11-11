"""Combos: positions on several outcomes at once, priced by request for quote."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.errors import InvalidRequest, Refused
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, WriteResult
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


# Writes


class ComboTrade(WriteResult):
    request_id: str | None = None
    quote: dict[str, Any] | None = None
    state: str | None = None


async def _describe_combo(
    call: Call,
    legs: list[str],
    direction: str,
    amount: Decimal,
    worst_price: Decimal,
    dry_run: bool = False,
) -> str:
    what = f"spend up to ${amount}" if direction == "BUY" else f"sell {amount} shares"
    bound = "at most" if direction == "BUY" else "at least"
    return (
        f"Request a Combo quote over {len(legs)} legs and accept it at once if its price is "
        f"{bound} {worst_price}: {direction}, {what}, from wallet {call.identity.wallet_address}?"
    )


def _e6(value: Decimal) -> str:
    units = value * 1_000_000
    if units != units.to_integral_value() or units <= 0:
        raise InvalidRequest("Amounts have at most 6 decimals and must be positive.")
    return str(int(units))


@operation(
    "Trade a Combo",
    toolset=TOOLSET,
    requires=("clob_l2", "signer"),
    gate="trading",
    destructive=True,
    experimental=True,
    describe=_describe_combo,
)
async def trade_combo(
    call: Call,
    legs: Annotated[
        list[str],
        Field(
            min_length=2,
            max_length=50,
            description="Outcome position ids, from list_combo_markets.",
        ),
    ],
    direction: Literal["BUY", "SELL"],
    amount: Annotated[
        Decimal, Field(gt=0, description="BUY: pUSD budget incl. fees. SELL: shares.")
    ],
    worst_price: Annotated[
        Decimal, Field(gt=0, lt=1, description="BUY: highest price; SELL: lowest price.")
    ],
    dry_run: Annotated[bool, Field(description="Only describe the request.")] = False,
) -> ComboTrade:
    """Buy or sell a Combo (all legs must happen): request a quote from market makers and accept
    it within its five-second window when the price is within worst_price. The quote is
    accepted in the same call because the window is too short for another round trip."""
    from mcp_server_polymarket import safety
    from mcp_server_polymarket.ops.trading import signer_of

    if dry_run:
        return ComboTrade(status="preview", dry_run=True, message=f"{direction} {amount}")
    settings = call.settings
    if direction == "BUY":
        safety.check_limit(settings.max_order_usd, amount, "A Combo", "POLYMARKET_MAX_ORDER_USD")
        safety.check_daily(call, amount, "trading")
    await safety.check_geoblock(call)
    signer = signer_of(call)
    if signer.session_key:
        raise Refused("Combos are not supported for session keys yet; use the owner's key.")
    request = {
        "signer_address": signer.wallet if signer.kind == 3 else signer.address,
        "maker_address": signer.wallet,
        "signature_type": signer.kind,
        "leg_position_ids": legs,
        "direction": direction,
        "side": "YES",
        "requested_size": {
            "unit": "notional" if direction == "BUY" else "shares",
            "value_e6": _e6(amount),
        },
    }
    created, unknown = await safety.send_write(
        call,
        tool="trading:trade_combo",
        endpoint=ep.COMBO_REQUEST_CREATE,
        body=request,
        summary=f"combo {direction} {amount} over {len(legs)} legs",
        usd=amount if direction == "BUY" else None,
        print_=safety.fingerprint("combo", legs, direction, str(amount)),
    )
    if unknown:
        return ComboTrade(**unknown.model_dump())
    created = created if isinstance(created, dict) else {}
    request_id = created.get("rfq_id")
    quote = created.get("quote") or {}
    if created.get("status") != "AWAITING_REQUESTER_ACCEPTANCE" or not quote:
        return ComboTrade(
            status="no_quote", request_id=request_id, message=str(created.get("error") or created)
        )
    price = Decimal(str(quote.get("blended_price_e6", "0"))) / 1_000_000
    if (direction == "BUY" and price > worst_price) or (
        direction == "SELL" and price < worst_price
    ):
        return ComboTrade(
            status="declined",
            request_id=request_id,
            quote=quote,
            message=f"The quote price {price} is outside {worst_price}; it was not accepted.",
        )
    position = (created.get("request") or {}).get("yes_position_id")
    signed = _acceptance_order(signer, direction, str(position), quote)
    accepted = await call.json(
        ep.COMBO_REQUEST_ACCEPT,
        path_args={"id": str(request_id)},
        body={"quote_id": quote.get("quote_id"), "signed_order": signed},
    )
    accepted = accepted if isinstance(accepted, dict) else {}
    return ComboTrade(
        status="accepted" if accepted.get("status") != "FAILED" else "failed",
        request_id=request_id,
        quote=quote,
        state=accepted.get("status"),
        message=str(accepted.get("error")) if accepted.get("error") else None,
        next_step="Follow it with get_combo_request until FILLED or CONFIRMED.",
    )


def _acceptance_order(
    signer: Any, direction: str, position: str, quote: dict[str, Any]
) -> dict[str, Any]:
    """An Exchange V3 order for the Combo's YES position, timestamped in seconds."""
    import secrets
    import time

    from mcp_server_polymarket.signing.orders import Order, sign_order
    from mcp_server_polymarket.signing.wallets import EXCHANGE_V3

    order = Order(
        token_id=position,
        side="BUY" if direction == "BUY" else "SELL",
        maker=signer.wallet,
        signer=signer.wallet if signer.kind == 3 else signer.address,
        maker_amount=int(quote["maker_amount_e6"]),
        taker_amount=int(quote["taker_amount_e6"]),
        signature_type=signer.kind,
        salt=secrets.randbits(64),
        timestamp_ms=int(time.time()),
        exchange=EXCHANGE_V3,
        version="3",
    )
    signed = sign_order(signer.key, order)
    return {
        "salt": str(order.salt),
        "maker": order.maker,
        "signer": order.signer,
        "tokenId": order.token_id,
        "makerAmount": str(order.maker_amount),
        "takerAmount": str(order.taker_amount),
        "side": 0 if order.side == "BUY" else 1,
        "signatureType": order.signature_type,
        "timestamp": str(order.timestamp_ms),
        "builder": order.builder,
        "metadata": order.metadata,
        "signature": signed.signature,
    }


async def _describe_return(call: Call, plan_hash: str) -> str:
    return (
        f"Release the pUSD planned by collateral return plan {plan_hash[:12]}… back to wallet "
        f"{call.identity.wallet_address}?"
    )


@operation(
    "Execute a Combo collateral return",
    toolset=TOOLSET,
    requires=("owner_signer", "relayer_key"),
    gate="trading",
    experimental=True,
    describe=_describe_return,
)
async def execute_collateral_return(
    call: Call, plan_hash: Annotated[str, Field(description="From plan_collateral_return.")]
) -> WriteResult:
    """Execute a collateral return plan: the call is fetched again from Polymarket, checked to
    target the Polymarket Router, signed as a Deposit Wallet batch and submitted."""
    from mcp_server_polymarket import wallet_tx
    from mcp_server_polymarket.signing import relayer
    from mcp_server_polymarket.signing.wallets import ROUTER_V2

    wallet = call.identity.wallet_address
    body = await call.json(ep.COLLATERAL_RETURN_PLAN, body={"wallet": wallet}) or {}
    if body.get("plan_hash") != plan_hash:
        raise Refused(
            "The plan changed since it was shown.", hint="Call plan_collateral_return again."
        )
    router_call = body.get("router_call") or {}
    if str(router_call.get("to", "")).lower() != ROUTER_V2.lower():
        raise Refused("The plan does not target the Polymarket Router; refusing to sign it.")
    calls = [relayer.Call(target=ROUTER_V2, data=str(router_call.get("data")), value=0)]
    signed = await wallet_tx.sign_batch(call.client, call.identity, calls, deadline=call.deadline)
    envelope = relayer.batch_wire(calls=calls, metadata="Collateral return", **signed)
    data = await call.json(
        ep.COLLATERAL_RETURN_SUBMIT, body={"plan_hash": plan_hash, "envelope": envelope}
    )
    return WriteResult(
        status="submitted", raw=data, next_step="Follow it with get_relayer_transaction."
    )
