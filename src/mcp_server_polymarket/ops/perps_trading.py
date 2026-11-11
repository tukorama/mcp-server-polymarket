"""Perps writes: orders, leverage, margin and moving pUSD in and out of the Perps account.

Trading operations are signed by the session's trading key. Withdrawals, transfers and
deposits need the account owner's key, and the transfers gate.
"""

import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mcp_server_polymarket import safety, wallet_tx
from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.paging import epoch_ms
from mcp_server_polymarket.errors import ConfigError, InvalidRequest, NotFound, Refused
from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops.perps_market import instrument, perps_slim
from mcp_server_polymarket.ops.registry import Call, InstrumentId, When, operation

TOOLSET = "perps-trading"
TRADER = ("perps_session", "perps_signer")
OWNER = ("perps_owner",)
MIN_DEPOSIT = Decimal(10)
DryRun = Annotated[bool, Field(description="Check and price it without sending.")]
Amount = Annotated[str, Field(pattern=r"^\d+(\.\d+)?$", description="Decimal string, e.g. 0.5.")]
COID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", description="Your 32-hex-digit order id.")]


class PerpsResult(WriteResult):
    results: list[Any] | None = None
    preview: dict[str, Any] | None = None


def _decimal(value: str | None, what: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise InvalidRequest(f"{what} {value!r} is not a number.") from exc
    return number


def _check_number(value: Decimal, decimals: int | None, what: str) -> None:
    places = max(0, -int(value.normalize().as_tuple().exponent))  # type: ignore[arg-type]
    if decimals is not None and places > decimals:
        raise InvalidRequest(f"{what} {value} has more than {decimals} decimals for this market.")
    digits = len(value.normalize().as_tuple().digits)
    if digits > 5:
        raise InvalidRequest(f"{what} {value} has more than 5 significant figures.")


async def mark_of(call: Call, instrument_id: int) -> Decimal | None:
    body = await call.json(ep.PERPS_TICKERS, params={"instrument_id": instrument_id})
    rows = body if isinstance(body, list) else []
    for row in rows:
        if isinstance(row, dict) and row.get("instrument_id") == instrument_id:
            return _decimal(row.get("mark_price"), "mark")
    return None


def proxy_key(call: Call) -> str:
    from mcp_server_polymarket import signing

    signing.require()
    session = call.identity.perps_session()
    if session is None or not session.proxy_key:
        raise ConfigError(
            "This Perps session cannot sign.",
            hint="Run `mcp-server-polymarket auth --perps` without --read-only.",
        )
    return session.proxy_key


def owner_key(call: Call) -> tuple[str, str]:
    from mcp_server_polymarket import signing

    signing.require()
    identity = call.identity
    if not identity.signer_key or not identity.signer_address or identity.is_session_key:
        raise ConfigError("This needs the Perps account owner's private key.")
    return identity.signer_key, identity.signer_address


def now_ms() -> int:
    return int(time.time() * 1000)


async def send_op(
    call: Call,
    *,
    tool: str,
    endpoint: ep.Endpoint,
    op: list[Any],
    key: str,
    summary: str,
    usd: Decimal | None,
    print_: str | None,
) -> tuple[Any, WriteResult | None]:
    from mcp_server_polymarket.signing.perps import signed_request

    body = signed_request(key, op, salt=secrets.randbits(32), ts_ms=now_ms())
    return await safety.send_write(
        call, tool=tool, endpoint=endpoint, body=body, summary=summary, usd=usd, print_=print_
    )


def _outcome(data: Any) -> PerpsResult:
    if isinstance(data, dict) and data.get("status") == "err":
        return PerpsResult(status="rejected", message=str(data.get("error")), raw=data)
    if isinstance(data, list):
        errors = [r for r in data if isinstance(r, dict) and r.get("status") == "err"]
        status = (
            "rejected" if errors and len(errors) == len(data) else "partial" if errors else "done"
        )
        return PerpsResult(status=status, results=perps_slim(data))
    return PerpsResult(status="done", raw=perps_slim(data) if isinstance(data, dict) else data)


# Orders


class Trigger(BaseModel):
    trigger_price: Amount
    limit_price: Annotated[
        Amount | None, Field(description="Empty: execute at market when triggered.")
    ] = None


class PerpsOrderSpec(BaseModel):
    instrument_id: int
    side: Literal["BUY", "SELL"]
    quantity: Amount
    price: Annotated[
        Amount | None, Field(description="Limit price. Empty only for ioc or fok at market.")
    ] = None
    time_in_force: Literal["gtc", "ioc", "fok"] = "gtc"
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: COID | None = None
    take_profit: Trigger | None = None
    stop_loss: Trigger | None = None


async def _rows(call: Call, specs: list[PerpsOrderSpec]) -> tuple[list[Any], Decimal, list[str]]:
    from mcp_server_polymarket.signing import perps

    settings = call.settings
    rows: list[Any] = []
    total = Decimal(0)
    lines = []
    builder = (settings.perps_builder, "0.0001") if settings.perps_builder else None
    for spec in specs:
        info = await instrument(call, spec.instrument_id)
        if info.get("close_only") and not spec.reduce_only:
            raise Refused(f"{info.get('symbol')} is close-only: only reduce-only orders.")
        quantity = _decimal(spec.quantity, "quantity")
        price = _decimal(spec.price, "price")
        assert quantity is not None
        _check_number(quantity, info.get("quantity_decimals"), "quantity")
        if price is not None:
            _check_number(price, info.get("price_decimals"), "price")
        elif spec.time_in_force == "gtc":
            raise InvalidRequest("A gtc order needs a price; use ioc or fok to trade at market.")
        mark = await mark_of(call, spec.instrument_id)
        side = spec.side
        if price is not None:
            safety.check_deviation(
                price, mark, side, "Price", relative=True, limit=settings.max_price_deviation
            )
        reference = max(price or 0, mark or 0)
        if not reference and not spec.reduce_only:
            raise Refused(
                f"No mark price for {info.get('symbol')}, so this order's value cannot be checked "
                "against the limits.",
                hint="Give a limit price.",
            )
        notional = quantity * Decimal(reference)
        minimum = _decimal(info.get("min_notional"), "min_notional")
        if minimum is not None and notional and notional < minimum and not spec.reduce_only:
            raise InvalidRequest(f"Order value ${notional:.2f} is below the ${minimum} minimum.")
        if not spec.reduce_only:
            total += notional
        rows.append(
            perps.order_row(
                perps.PerpsOrder(
                    instrument_id=spec.instrument_id,
                    buy=side == "BUY",
                    quantity=spec.quantity,
                    price=spec.price,
                    tif=spec.time_in_force,
                    post_only=spec.post_only,
                    reduce_only=spec.reduce_only,
                    client_order_id=spec.client_order_id,
                    builder=builder,
                )
            )
        )
        at = f"at {spec.price}" if spec.price else "at market"
        flags = [
            n for n, on in (("reduce-only", spec.reduce_only), ("post-only", spec.post_only)) if on
        ]
        extra = "".join(f", {flag}" for flag in flags)
        lines.append(
            f"{side} {spec.quantity} {info.get('symbol')} {at} ({spec.time_in_force}{extra})"
        )
        for kind, trigger in (("tp", spec.take_profit), ("sl", spec.stop_loss)):
            if trigger is None:
                continue
            for value, what in (
                (trigger.trigger_price, "trigger price"),
                (trigger.limit_price, "limit price"),
            ):
                number = _decimal(value, what)
                if number is not None:
                    _check_number(number, info.get("price_decimals"), what)
            rows.append(
                perps.order_row(
                    perps.PerpsOrder(
                        instrument_id=spec.instrument_id,
                        buy=side != "BUY",
                        quantity=spec.quantity,
                        reduce_only=True,
                        trigger=perps.Trigger(kind, trigger.trigger_price, trigger.limit_price),  # type: ignore[arg-type]
                        builder=builder,
                    )
                )
            )
            name = "take profit" if kind == "tp" else "stop loss"
            then = f", then limit {trigger.limit_price}" if trigger.limit_price else ", then market"
            lines.append(f"  {name} triggered at {trigger.trigger_price}{then} (reduce-only)")
    return rows, total, lines


async def _describe_orders(call: Call, orders: list[Any], dry_run: bool = False) -> str:
    specs = [
        o if isinstance(o, PerpsOrderSpec) else PerpsOrderSpec.model_validate(o) for o in orders
    ]
    _, total, lines = await _rows(call, specs)
    return (
        f"Place these Perps orders (about ${total:.2f} of exposure):\n- "
        + "\n- ".join(lines)
        + "\nSend them?"
    )


@operation(
    "Place Perps orders",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    destructive=True,
    describe=_describe_orders,
)
async def place_perps_orders(
    call: Call,
    orders: Annotated[list[PerpsOrderSpec], Field(min_length=1, max_length=10)],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Place Perps orders. An order with take_profit or stop_loss is sent as a bracket: the
    entry plus reduce-only exit orders that trigger on the mark price. Prices and quantities are
    decimal strings within the instrument's decimals (list_perps_instruments). Checked against
    the mark price and the USD limits; the user confirms. Only on the user's explicit request."""
    from mcp_server_polymarket.signing import perps

    brackets = any(o.take_profit or o.stop_loss for o in orders)
    if brackets and len(orders) > 1:
        raise InvalidRequest("Send an order with take_profit or stop_loss on its own.")
    rows, total, lines = await _rows(call, orders)
    if dry_run:
        return PerpsResult(
            status="preview",
            dry_run=True,
            preview={"orders": lines, "exposure_usd": f"{total:.2f}"},
        )
    settings = call.settings
    await safety.check_geoblock(call)  # first: nothing may await between check_daily and send
    safety.check_limit(
        settings.max_perps_order_usd, total, "This order", "POLYMARKET_MAX_PERPS_ORDER_USD"
    )
    safety.check_daily(call, total, "perps")
    print_ = safety.fingerprint("perps-orders", [o.model_dump() for o in orders])
    safety.block_duplicate(call, print_)
    op = perps.create_orders(rows, "order" if brackets else None)
    data, unknown = await send_op(
        call,
        tool="perps:place_perps_orders",
        endpoint=ep.PERPS_CREATE_ORDERS,
        op=op,
        key=proxy_key(call),
        summary="; ".join(lines),
        usd=total,
        print_=print_,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


class Modification(BaseModel):
    order_id: int | None = None
    client_order_id: COID | None = None
    price: Amount
    quantity: Annotated[Amount, Field(description="The new total quantity.")]


async def _modified(call: Call, changes: list[Modification]) -> tuple[Decimal, list[str]]:
    """Check each change like a new order, against the resting order it changes.

    Returns the exposure the changed orders add up to, and a line per change.
    """
    body = await call.json(ep.PERPS_OPEN_ORDERS)
    if isinstance(body, dict):
        body = next((v for v in body.values() if isinstance(v, list)), [])
    resting = [o for o in body or [] if isinstance(o, dict)]
    settings = call.settings
    total = Decimal(0)
    lines = []
    for change in changes:
        order = next(
            (
                o
                for o in resting
                if (change.order_id is not None and o.get("order_id") == change.order_id)
                or (change.client_order_id and o.get("client_order_id") == change.client_order_id)
            ),
            None,
        )
        name = change.order_id or change.client_order_id
        if order is None or not isinstance(order.get("instrument_id"), int):
            raise NotFound(f"Order {name} is not among your open Perps orders.")
        info = await instrument(call, order["instrument_id"])
        price, quantity = _decimal(change.price, "price"), _decimal(change.quantity, "quantity")
        assert price is not None and quantity is not None
        _check_number(price, info.get("price_decimals"), "price")
        _check_number(quantity, info.get("quantity_decimals"), "quantity")
        side = "BUY" if order.get("buy") else "SELL"
        mark = await mark_of(call, order["instrument_id"])
        safety.check_deviation(
            price, mark, side, "Price", relative=True, limit=settings.max_price_deviation
        )
        if not order.get("ro"):
            total += quantity * max(price, mark or Decimal(0))
        lines.append(
            f"order {name} ({side} {info.get('symbol')}): price {order.get('price')} -> {price}, "
            f"quantity {order.get('quantity')} -> {quantity}"
        )
    return total, lines


async def _describe_modify(call: Call, changes: list[Any], dry_run: bool = False) -> str:
    items = [c if isinstance(c, Modification) else Modification.model_validate(c) for c in changes]
    total, lines = await _modified(call, items)
    return (
        f"Change these Perps orders (about ${total:.2f} of exposure afterwards)?\n- "
        + "\n- ".join(lines)
    )


@operation(
    "Modify Perps orders",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    experimental=True,
    describe=_describe_modify,
)
async def modify_perps_orders(
    call: Call,
    changes: Annotated[list[Modification], Field(min_length=1, max_length=50)],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Change the price and total quantity of resting orders, by order id or by your client id
    (one kind per call)."""
    from mcp_server_polymarket.signing import perps

    by_id = [c for c in changes if c.order_id is not None]
    by_coid = [c for c in changes if c.order_id is None and c.client_order_id]
    if by_id and by_coid or len(by_id) + len(by_coid) != len(changes):
        raise InvalidRequest("Give each change an order_id, or each a client_order_id.")
    if by_id:
        op = perps.modify_orders([(c.order_id, c.price, c.quantity) for c in by_id])  # type: ignore[misc]
        endpoint = ep.PERPS_MODIFY_ORDERS
    else:
        op = perps.modify_orders_coid([(c.client_order_id, c.price, c.quantity) for c in by_coid])  # type: ignore[misc]
        endpoint = ep.PERPS_MODIFY_ORDERS_COID
    total, lines = await _modified(call, changes)
    if dry_run:
        return PerpsResult(
            status="preview",
            dry_run=True,
            preview={"changes": lines, "exposure_usd": f"{total:.2f}", "op": perps.body_op(op)},
        )
    settings = call.settings
    await safety.check_geoblock(call)
    safety.check_limit(
        settings.max_perps_order_usd, total, "This change", "POLYMARKET_MAX_PERPS_ORDER_USD"
    )
    safety.check_daily(call, total, "perps")
    print_ = safety.fingerprint("perps-modify", [c.model_dump() for c in changes])
    safety.block_duplicate(call, print_)
    data, unknown = await send_op(
        call,
        tool="perps:modify_perps_orders",
        endpoint=endpoint,
        op=op,
        key=proxy_key(call),
        summary="; ".join(lines),
        usd=total,
        print_=print_,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


@operation(
    "Cancel Perps orders",
    toolset=TOOLSET,
    requires=TRADER,
    gate="cancel",
    idempotent=True,
)
async def cancel_perps_orders(
    call: Call,
    order_ids: Annotated[list[int] | None, Field(description="Exchange order ids.")] = None,
    client_order_ids: list[COID] | None = None,
) -> PerpsResult:
    """Cancel resting Perps orders (and their TP/SL children) by id or client id."""
    from mcp_server_polymarket.signing import perps

    if bool(order_ids) == bool(client_order_ids):
        raise InvalidRequest("Give order_ids or client_order_ids.")
    if order_ids:
        op, endpoint = perps.cancel_orders(order_ids), ep.PERPS_CANCEL_ORDERS
    else:
        op, endpoint = perps.cancel_orders_coid(client_order_ids or []), ep.PERPS_CANCEL_ORDERS_COID
    data, unknown = await send_op(
        call,
        tool="perps:cancel_perps_orders",
        endpoint=endpoint,
        op=op,
        key=proxy_key(call),
        summary="cancel",
        usd=None,
        print_=None,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


@operation(
    "Cancel all Perps orders",
    toolset=TOOLSET,
    requires=TRADER,
    gate="cancel",
    idempotent=True,
    destructive=True,
)
async def cancel_all_perps_orders(
    call: Call, instrument_id: Annotated[int | None, Field(description="Only this one.")] = None
) -> PerpsResult:
    """Cancel every resting Perps order, or every order on one instrument."""
    from mcp_server_polymarket.signing import perps

    data, unknown = await send_op(
        call,
        tool="perps:cancel_all_perps_orders",
        endpoint=ep.PERPS_CANCEL_ALL,
        op=perps.cancel_all(instrument_id),
        key=proxy_key(call),
        summary="cancel all",
        usd=None,
        print_=None,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


@operation(
    "Set the Perps dead man's switch",
    toolset=TOOLSET,
    requires=TRADER,
    gate="cancel",
)
async def set_perps_auto_cancel(
    call: Call,
    cancel_at: Annotated[
        When, Field(description="When to cancel all orders, at least 5 s ahead. Empty disarms.")
    ] = None,
) -> PerpsResult:
    """Arm a one-shot cancel-all at a time (renew it to keep orders alive), or disarm it. The
    switch can fire a limited number of times per UTC day."""
    from mcp_server_polymarket.signing import perps

    time_ms = epoch_ms(cancel_at) if cancel_at else 0
    if time_ms and time_ms < now_ms() + 5000:
        raise InvalidRequest("cancel_at must be at least 5 seconds from now.")
    data, unknown = await send_op(
        call,
        tool="perps:set_perps_auto_cancel",
        endpoint=ep.PERPS_AUTO_CANCEL,
        op=perps.auto_cancel(time_ms or 0),
        key=proxy_key(call),
        summary=f"auto-cancel {time_ms}",
        usd=None,
        print_=None,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


class LeverageChange(BaseModel):
    instrument_id: int
    leverage: Annotated[int, Field(ge=1)]
    cross_margin: Annotated[bool, Field(description="false: isolated margin.")] = True


async def _describe_leverage(call: Call, changes: list[Any], dry_run: bool = False) -> str:
    items = [
        c if isinstance(c, LeverageChange) else LeverageChange.model_validate(c) for c in changes
    ]
    lines = [
        f"instrument {c.instrument_id}: {c.leverage}x {'cross' if c.cross_margin else 'isolated'}"
        for c in items
    ]
    return "Set Perps leverage?\n- " + "\n- ".join(lines)


@operation(
    "Set Perps leverage",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    describe=_describe_leverage,
)
async def set_perps_leverage(
    call: Call,
    changes: Annotated[list[LeverageChange], Field(min_length=1, max_length=100)],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Set leverage and margin mode per instrument. Capped by POLYMARKET_MAX_PERPS_LEVERAGE and
    the instrument's own maximum."""
    from mcp_server_polymarket.signing import perps

    cap = call.settings.max_perps_leverage
    for change in changes:
        info = await instrument(call, change.instrument_id)
        maximum = info.get("max_leverage")
        if maximum and change.leverage > int(maximum):
            raise InvalidRequest(f"{info.get('symbol')} allows at most {maximum}x.")
        if cap != "unlimited" and Decimal(change.leverage) > cap:
            raise Refused(
                f"{change.leverage}x is above the limit of {cap}x (POLYMARKET_MAX_PERPS_LEVERAGE)."
            )
    if len(changes) == 1:
        c = changes[0]
        op, endpoint = (
            perps.update_leverage(c.instrument_id, c.leverage, c.cross_margin),
            ep.PERPS_LEVERAGE,
        )
    else:
        op = perps.update_leverages(
            [(c.instrument_id, c.leverage, c.cross_margin) for c in changes]
        )
        endpoint = ep.PERPS_LEVERAGE_BATCH
    if dry_run:
        return PerpsResult(status="preview", dry_run=True, preview={"op": perps.body_op(op)})
    data, unknown = await send_op(
        call,
        tool="perps:set_perps_leverage",
        endpoint=endpoint,
        op=op,
        key=proxy_key(call),
        summary="leverage",
        usd=None,
        print_=None,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


async def _describe_margin(
    call: Call, instrument_id: int, amount: str, dry_run: bool = False
) -> str:
    verb = "Remove" if amount.startswith("-") else "Add"
    return f"{verb} {amount.lstrip('-+')} pUSD of isolated margin on instrument {instrument_id}?"


@operation(
    "Adjust isolated margin",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    describe=_describe_margin,
)
async def adjust_perps_margin(
    call: Call,
    instrument_id: InstrumentId,
    amount: Annotated[
        str, Field(pattern=r"^[+-]?\d+(\.\d+)?$", description="pUSD; negative removes margin.")
    ],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Add margin to, or remove it from, an isolated position."""
    from mcp_server_polymarket.signing import perps

    op = perps.update_margin(instrument_id, amount.lstrip("+"))
    if dry_run:
        return PerpsResult(status="preview", dry_run=True, preview={"op": perps.body_op(op)})
    magnitude = abs(Decimal(amount))
    safety.check_limit(
        call.settings.max_perps_order_usd,
        magnitude,
        "A margin change",
        "POLYMARKET_MAX_PERPS_ORDER_USD",
    )
    data, unknown = await send_op(
        call,
        tool="perps:adjust_perps_margin",
        endpoint=ep.PERPS_MARGIN,
        op=op,
        key=proxy_key(call),
        summary=f"margin {amount} on {instrument_id}",
        usd=None,
        print_=safety.fingerprint("margin", instrument_id, amount),
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


# Account housekeeping


@operation("Mark Perps notifications read", toolset=TOOLSET, requires=("perps_session",))
async def mark_perps_notifications_read(
    call: Call,
    notification_ids: list[str] | None = None,
    up_to_cursor: Annotated[
        str | None, Field(description="Mark everything up to this cursor.")
    ] = None,
) -> WriteResult:
    """Mark Perps notifications read, by id or everything up to a cursor."""
    if bool(notification_ids) == bool(up_to_cursor):
        raise InvalidRequest("Give notification_ids or up_to_cursor.")
    body = {"ids": notification_ids} if notification_ids else {"before": up_to_cursor}
    data = await call.json(ep.PERPS_NOTIFICATIONS_READ, body=body)
    return WriteResult(status="done", raw=data)


@operation(
    "Get my Perps invite code", toolset=TOOLSET, requires=("perps_session",), idempotent=True
)
async def create_perps_invite_code(call: Call) -> WriteResult:
    """Your Perps invite code, created on first use."""
    data = await call.json(ep.PERPS_CREATE_INVITE, body={})
    return WriteResult(status="done", raw=data)


async def _describe_referral(call: Call, code: str) -> str:
    return f"Apply referral code {code} to your Perps account? This can only be done once."


@operation(
    "Apply a Perps referral code",
    toolset=TOOLSET,
    requires=("perps_session",),
    gate="trading",
    describe=_describe_referral,
)
async def apply_perps_referral_code(call: Call, code: str) -> WriteResult:
    """Attach a referral code to the account. One time only; it cannot be changed."""
    data = await call.json(ep.PERPS_APPLY_REFERRAL, body={"code": code})
    return WriteResult(status="done", raw=data)


# Moving funds


def _units(amount: Decimal) -> int:
    units = amount * 1_000_000
    if units != units.to_integral_value() or units <= 0:
        raise InvalidRequest("Amounts have at most 6 decimals and must be positive.")
    return int(units)


async def _describe_deposit(call: Call, amount: Decimal, dry_run: bool = False) -> str:
    return (
        f"Move {amount} pUSD from wallet {call.identity.wallet_address} into the Perps account "
        f"of {call.identity.signer_address}?"
    )


@operation(
    "Deposit pUSD into Perps",
    toolset=TOOLSET,
    requires=("owner_signer", "relayer_key"),
    gate="transfers",
    destructive=True,
    describe=_describe_deposit,
)
async def deposit_to_perps(
    call: Call,
    amount: Annotated[Decimal, Field(ge=MIN_DEPOSIT, description="pUSD, at least 10.")],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Move pUSD from the Polymarket Deposit Wallet to the Perps account of its owner, in one
    gasless transaction with an exact approval."""
    from mcp_server_polymarket.signing import relayer
    from mcp_server_polymarket.signing import wallets as w

    units = _units(amount)
    owner = call.identity.signer_address
    if owner is None:
        raise ConfigError("No owner key configured.")
    calls = [
        relayer.erc20_approve(w.PUSD, w.PERPS_DEPOSIT_CONTRACT, units),
        relayer.perps_deposit(units, owner),
    ]
    if dry_run:
        return PerpsResult(
            status="preview", dry_run=True, preview={"amount": str(amount), "to": owner}
        )
    safety.check_limit(
        call.settings.max_transfer_usd, amount, "A deposit", "POLYMARKET_MAX_TRANSFER_USD"
    )
    safety.check_daily(call, amount, "transfers")
    print_ = safety.fingerprint("perps-deposit", str(amount), owner)
    safety.block_duplicate(call, print_)
    tx = await wallet_tx.submit(
        call.client,
        call.identity,
        calls,
        metadata="Perps deposit",
        wait_seconds=call.limit_wait(30),
        deadline=call.deadline,
        record=wallet_tx.Record(
            journal=call.runtime.journal,
            tool="transfers:deposit_to_perps",
            summary=f"deposit {amount} pUSD to the Perps account of {owner}",
            usd=amount,
            fingerprint=print_,
        ),
    )
    if tx.state == "UNKNOWN":
        return PerpsResult(
            **safety.unknown_result(tx.request_id or "", tx.error or "no answer").model_dump()
        )
    status = (
        "confirmed"
        if tx.state == "STATE_CONFIRMED"
        else "failed"
        if tx.state in wallet_tx.TERMINAL
        else "pending"
    )
    return PerpsResult(
        status=status,
        message=tx.error,
        raw={"transaction_id": tx.transaction_id, "state": tx.state, "hash": tx.transaction_hash},
        next_step="It appears in list_my_perps_transfers once confirmed."
        if status != "failed"
        else None,
    )


def _withdraw_recipient(call: Call, to: str | None) -> tuple[str, list[str | None]]:
    """(recipient, the user's own addresses). Shared by the confirmation and the signed body."""
    owner = call.identity.signer_address
    wallet = call.identity.verified_wallet
    recipient = to or wallet or owner
    if not recipient:
        raise ConfigError("Withdrawals need the owner's key.", hint="Set POLYMARKET_PRIVATE_KEY.")
    return recipient, [wallet, owner]


async def _describe_withdraw(
    call: Call, amount: Decimal, to: str | None = None, dry_run: bool = False
) -> str:
    recipient, _ = _withdraw_recipient(call, to)
    return f"Withdraw {amount} pUSD from the Perps account to {recipient}? This sends funds out."


@operation(
    "Withdraw pUSD from Perps",
    toolset=TOOLSET,
    requires=OWNER,
    gate="transfers",
    destructive=True,
    describe=_describe_withdraw,
)
async def withdraw_from_perps(
    call: Call,
    amount: Annotated[Decimal, Field(gt=0, description="pUSD to withdraw.")],
    to: Annotated[
        str | None,
        Field(description="Recipient. Empty: your Polymarket wallet. Others must be allowlisted."),
    ] = None,
    dry_run: DryRun = False,
) -> PerpsResult:
    """Withdraw pUSD from the Perps account, signed by the owner. The recipient is your own
    wallet unless the user allowlisted another (POLYMARKET_TRANSFER_ALLOWLIST)."""
    from mcp_server_polymarket.signing import eip712
    from mcp_server_polymarket.signing.perps import withdraw_typed_data
    from mcp_server_polymarket.signing.wallets import PUSD

    key, owner = owner_key(call)
    recipient, own = _withdraw_recipient(call, to)
    safety.check_recipient(call, recipient, own)
    units = _units(amount)
    if dry_run:
        return PerpsResult(
            status="preview", dry_run=True, preview={"amount": str(amount), "to": recipient}
        )
    safety.check_limit(
        call.settings.max_transfer_usd, amount, "A withdrawal", "POLYMARKET_MAX_TRANSFER_USD"
    )
    print_ = safety.fingerprint("withdraw", str(amount), recipient)
    safety.check_daily(call, amount, "transfers")
    safety.block_duplicate(call, print_)
    salt, ts = secrets.randbits(32), int(time.time())
    signature = eip712.sign(key, withdraw_typed_data(owner, PUSD, units, recipient, salt, ts))
    body = {
        "op": {
            "type": "withdraw",
            "args": {"account": owner, "token": PUSD, "amount": str(units), "to": recipient},
        },
        "salt": salt,
        "sig": signature,
        "ts": ts,
    }
    data, unknown = await safety.send_write(
        call,
        tool="transfers:withdraw_from_perps",
        endpoint=ep.PERPS_WITHDRAW,
        body=body,
        summary=f"withdraw {amount} pUSD to {recipient}",
        usd=amount,
        print_=print_,
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)


async def _describe_transfer(call: Call, amount: str, to: str, dry_run: bool = False) -> str:
    return f"Transfer {amount} pUSD from your Perps account to the Perps account {to}?"


@operation(
    "Transfer pUSD to another Perps account",
    toolset=TOOLSET,
    requires=OWNER,
    gate="transfers",
    destructive=True,
    experimental=True,
    describe=_describe_transfer,
)
async def transfer_perps_internal(
    call: Call,
    amount: Amount,
    to: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$", description="Allowlisted account.")],
    dry_run: DryRun = False,
) -> PerpsResult:
    """Move pUSD between Perps accounts on the exchange ledger, signed by the owner. The
    recipient must be on POLYMARKET_TRANSFER_ALLOWLIST."""
    from mcp_server_polymarket.signing import perps
    from mcp_server_polymarket.signing.wallets import PUSD

    key, owner = owner_key(call)
    safety.check_recipient(call, to, [])
    value = Decimal(amount)
    op = perps.internal_transfer(owner, PUSD, amount, to)
    if dry_run:
        return PerpsResult(status="preview", dry_run=True, preview={"op": perps.body_op(op)})
    safety.check_limit(
        call.settings.max_transfer_usd, value, "A transfer", "POLYMARKET_MAX_TRANSFER_USD"
    )
    safety.check_daily(call, value, "transfers")
    data, unknown = await send_op(
        call,
        tool="transfers:transfer_perps_internal",
        endpoint=ep.PERPS_INTERNAL_TRANSFER,
        op=op,
        key=key,
        summary=f"transfer {amount} pUSD to {to}",
        usd=value,
        print_=safety.fingerprint("transfer", amount, to),
    )
    return PerpsResult(**unknown.model_dump()) if unknown else _outcome(data)
