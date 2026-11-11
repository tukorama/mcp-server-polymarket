"""Prediction market orders. Every order is checked, confirmed by the user, then signed."""

import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mcp_server_polymarket import safety
from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.cache import MINUTE
from mcp_server_polymarket.client.paging import epoch_seconds
from mcp_server_polymarket.errors import ConfigError, InvalidRequest, NotFound, Refused
from mcp_server_polymarket.models.base import CompactModel, WriteResult, json_list
from mcp_server_polymarket.models.clob import OrderBook
from mcp_server_polymarket.ops.registry import Call, TokenId, When, operation
from mcp_server_polymarket.protocol import is_v2_token
from mcp_server_polymarket.redaction import clean_label

TOOLSET = "trading"
TRADER = ("clob_l2", "signer")
Side = Literal["BUY", "SELL"]
MIN_GTD_SECONDS = 180
MAX_BATCH = 15
Price = Annotated[Decimal, Field(gt=0, lt=1, description="Price per share in pUSD, e.g. 0.45.")]
Shares = Annotated[
    Decimal,
    Field(gt=0, decimal_places=2, description="Number of shares (outcome tokens), 2 decimals."),
]
DryRun = Annotated[bool, Field(description="Check and price the order without sending it.")]


class OrderPreview(CompactModel):
    market: str | None = None
    outcome: str | None = None
    token_id: str
    side: str
    order_type: str
    price: str
    size: str | None = None
    spend_usd: str | None = None
    notional_usd: str
    maker_amount: int | None = None
    taker_amount: int | None = None
    tick_size: str | None = None
    min_order_size: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    neg_risk: bool | None = None
    protocol: str | None = None
    expires: str | None = None


class OrderResult(WriteResult):
    order_id: str | None = None
    order_status: str | None = None
    making_amount: str | None = None
    taking_amount: str | None = None
    trade_ids: list[str] | None = None
    preview: OrderPreview | None = None


class OrdersResult(WriteResult):
    orders: list[OrderResult] | None = None


# Market facts


async def market_label(call: Call, token_id: str) -> tuple[str | None, str | None]:
    """(question, outcome) of a token, for confirmations. Cached; best effort."""
    cache = call.runtime.cache
    cached = cache.get(f"label:{token_id}", 10 * MINUTE)
    if cached is not None:
        return cached[0], cached[1]
    try:
        body = await call.json(
            ep.GAMMA_MARKETS_KEYSET,
            params={"clob_token_ids": token_id, "closed": "any", "limit": 1},
        )
    except Exception:
        return None, None
    rows = (body or {}).get("markets") or [] if isinstance(body, dict) else []
    if not rows:
        return None, None
    market = rows[0]
    tokens = json_list(market.get("clobTokenIds")) + json_list(market.get("positionIds"))
    names = json_list(market.get("outcomes"))
    outcome = None
    for index, token in enumerate(tokens):
        if str(token) == token_id and names:
            outcome = str(names[index % len(names)])
    label = (clean_label(market.get("question")), clean_label(outcome))
    cache.set(f"label:{token_id}", list(label), disk=False)
    return label


async def book_of(call: Call, token_id: str) -> OrderBook:
    raw = await call.json(ep.CLOB_BOOK, params={"token_id": token_id})
    if not isinstance(raw, dict) or not raw.get("asset_id"):
        raise NotFound(f"No order book for token {token_id}; the market may be closed.")
    book = OrderBook.from_api(raw)
    v2 = is_v2_token(token_id)
    if (book.protocol == "v2") != v2 and book.protocol is not None:
        raise Refused(
            f"Token {token_id} looks like a {'V2' if v2 else 'CTF'} token but its book says "
            f"{book.protocol}; refusing to sign for the wrong exchange."
        )
    return book


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass
class Signer:
    key: str
    address: str
    wallet: str
    kind: int
    session_key: bool
    owner_api_key: str


def signer_of(call: Call) -> Signer:
    from mcp_server_polymarket import signing

    signing.require()
    identity = call.identity
    credentials = identity.clob_credentials()
    if not (identity.signer_key and identity.signer_address and identity.wallet_address):
        raise ConfigError(
            "Orders need a signing key and wallet.",
            hint="Set POLYMARKET_PRIVATE_KEY (or _FILE) and POLYMARKET_WALLET_ADDRESS.",
        )
    if credentials is None:
        raise ConfigError(
            "Orders need CLOB API credentials.", hint="Run `mcp-server-polymarket auth`."
        )
    if credentials.address.lower() != identity.signer_address.lower():
        raise ConfigError(
            "The CLOB API credentials belong to another address than the signing key.",
            hint="Run `mcp-server-polymarket auth` with this key.",
        )
    kind = identity.wallet_kind
    assert kind is not None
    return Signer(
        key=identity.signer_key,
        address=identity.signer_address,
        wallet=identity.wallet_address,
        kind=kind,
        session_key=identity.is_session_key,
        owner_api_key=credentials.api_key,
    )


@dataclass
class Draft:
    token_id: str
    side: Side
    order_type: str
    maker_amount: int
    taker_amount: int
    neg_risk: bool
    expiration: int
    post_only: bool
    preview: OrderPreview
    usd: Decimal


async def draft_limit(
    call: Call,
    *,
    token_id: str,
    side: Side,
    price: Decimal,
    size: Decimal,
    expires_at: str | None,
    post_only: bool,
) -> Draft:
    from mcp_server_polymarket.signing.orders import check_price, limit_amounts

    book = await book_of(call, token_id)
    tick = _decimal(book.tick_size) or Decimal("0.01")
    check_price(price, tick)
    minimum = _decimal(book.min_order_size)
    if minimum is not None and size < minimum:
        raise InvalidRequest(f"Size {size} is below this market's minimum of {minimum} shares.")
    best = _decimal(book.best_ask if side == "BUY" else book.best_bid)
    safety.check_deviation(
        price, best, side, "Price", relative=False, limit=call.settings.max_price_deviation
    )
    maker, taker = limit_amounts(side, price, size, tick)
    # What the order can commit: a BUY spends its pUSD; a SELL can fill above its price.
    usd = Decimal(maker if side == "BUY" else taker) / 1_000_000
    if side == "SELL" and best is not None and best > price:
        usd = Decimal(maker) / 1_000_000 * best
    expiration = 0
    if expires_at:
        expiration = epoch_seconds(expires_at) or 0
        if expiration < time.time() + MIN_GTD_SECONDS:
            raise InvalidRequest("expires_at must be at least 3 minutes from now.")
    if (
        post_only
        and best is not None
        and ((side == "BUY" and price >= best) or (side == "SELL" and price <= best))
    ):
        raise InvalidRequest("A post-only order at this price would cross the book.")
    question, outcome = await market_label(call, token_id)
    preview = OrderPreview(
        market=question,
        outcome=outcome,
        token_id=token_id,
        side=side,
        order_type="GTD" if expiration else "GTC",
        price=format(price, "f"),
        size=format(size, "f"),
        notional_usd=format(usd.quantize(Decimal("0.01")), "f"),
        maker_amount=maker,
        taker_amount=taker,
        tick_size=format(tick, "f"),
        min_order_size=None if minimum is None else format(minimum, "f"),
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        neg_risk=book.neg_risk,
        protocol="v2" if is_v2_token(token_id) else "ctf",
        expires=expires_at,
    )
    return Draft(
        token_id=token_id,
        side=side,
        order_type=preview.order_type,
        maker_amount=maker,
        taker_amount=taker,
        neg_risk=bool(book.neg_risk),
        expiration=expiration,
        post_only=post_only,
        preview=preview,
        usd=usd,
    )


def sign_draft(call: Call, signer: Signer, draft: Draft) -> dict[str, Any]:
    from mcp_server_polymarket.signing.orders import build_order, order_wire, sign_order
    from mcp_server_polymarket.signing.wallets import WalletKind

    order = build_order(
        token_id=draft.token_id,
        side=draft.side,
        maker_amount=draft.maker_amount,
        taker_amount=draft.taker_amount,
        wallet=signer.wallet,
        signer=signer.address,
        kind=WalletKind(signer.kind),
        neg_risk=draft.neg_risk,
        salt=secrets.randbits(53),
        timestamp_ms=int(time.time() * 1000),
        expiration=draft.expiration,
        builder=call.settings.builder_code,
    )
    signed = sign_order(signer.key, order, session_key=signer.session_key)
    return order_wire(
        signed, owner=signer.owner_api_key, order_type=draft.order_type, post_only=draft.post_only
    )


def outcome_of(data: Any) -> OrderResult:
    """POST /order answers 200 even for some rejections, with success false or errorMsg set."""
    data = data if isinstance(data, dict) else {}
    error = data.get("errorMsg") or data.get("error")
    if data.get("success") is False or error:
        return OrderResult(status="rejected", message=str(error or "rejected"), raw=data)
    return OrderResult(
        status="placed",
        order_id=data.get("orderID") or data.get("orderId"),
        order_status=data.get("status"),
        making_amount=str(data["makingAmount"]) if data.get("makingAmount") is not None else None,
        taking_amount=str(data["takingAmount"]) if data.get("takingAmount") is not None else None,
        trade_ids=data.get("tradeIDs") or None,
    )


def _rejected(data: Any) -> bool:
    return outcome_of(data).status == "rejected"


async def _before_send(call: Call, usd: Decimal, print_: str) -> None:
    settings = call.settings
    # The geoblock check awaits, so it runs first: nothing may await between the daily-limit
    # check and the journal entry, or two concurrent orders could both pass the limit.
    await safety.check_geoblock(call)
    safety.check_limit(settings.max_order_usd, usd, "An order", "POLYMARKET_MAX_ORDER_USD")
    safety.check_daily(call, usd, "trading")
    safety.block_duplicate(call, print_)


# Descriptions shown to the user for confirmation. Deterministic: arguments and cached labels.


async def _describe_limit(
    call: Call,
    token_id: str,
    side: str,
    price: Decimal,
    size: Decimal,
    expires_at: str | None = None,
    post_only: bool = False,
    dry_run: bool = False,
) -> str:
    question, outcome = await market_label(call, token_id)
    what = f"'{outcome}' in '{question}'" if question else f"token {token_id}"
    total = (price * size).quantize(Decimal("0.01"))
    verb = "pay up to" if side == "BUY" else "receive at least"
    lasting = f"until {expires_at}" if expires_at else "until cancelled"
    extra = ", post-only" if post_only else ""
    return (
        f"{side} {size} shares of {what} at {price} pUSD each: {verb} ${total}, limit order "
        f"{lasting}{extra}. Wallet {call.identity.wallet_address}. Place this order?"
    )


async def _describe_market(
    call: Call,
    token_id: str,
    side: str,
    amount: Decimal,
    price_bound: Decimal,
    order_type: str = "FAK",
    dry_run: bool = False,
) -> str:
    question, outcome = await market_label(call, token_id)
    what = f"'{outcome}' in '{question}'" if question else f"token {token_id}"
    if side == "BUY":
        body = f"Spend up to ${amount} buying {what} at no more than {price_bound} pUSD per share"
    else:
        body = f"Sell {amount} shares of {what} at no less than {price_bound} pUSD per share"
    fill = "fill completely or not at all" if order_type == "FOK" else "unfilled rest cancelled"
    return f"{body} ({order_type}: {fill}). Wallet {call.identity.wallet_address}. Place it?"


async def _describe_batch(call: Call, orders: list[Any], dry_run: bool = False) -> str:
    lines = []
    total = Decimal(0)
    for spec in orders:
        spec = spec if isinstance(spec, LimitOrderSpec) else LimitOrderSpec.model_validate(spec)
        question, outcome = await market_label(call, spec.token_id)
        what = f"'{outcome}' in '{question}'" if question else f"token {spec.token_id}"
        total += spec.price * spec.size
        lines.append(f"- {spec.side} {spec.size} of {what} at {spec.price}")
    return (
        f"Place {len(lines)} limit orders (about ${total.quantize(Decimal('0.01'))} in total) "
        f"from {call.identity.wallet_address}?\n" + "\n".join(lines)
    )


@operation(
    "Place a limit order",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    destructive=True,
    describe=_describe_limit,
)
async def place_limit_order(
    call: Call,
    token_id: TokenId,
    side: Side,
    price: Price,
    size: Shares,
    expires_at: Annotated[
        When, Field(description="Good-till-date expiry, at least 3 minutes ahead. Empty: GTC.")
    ] = None,
    post_only: Annotated[bool, Field(description="Only rest on the book, never take.")] = False,
    dry_run: DryRun = False,
) -> OrderResult:
    """Buy or sell `size` shares of an outcome at `price` or better. The order rests on the book
    until filled, cancelled or expired. Checked against the tick size, minimum size, distance
    from the best price and the configured USD limits; the user confirms before it is signed.
    Only place orders the user explicitly asked for."""
    draft = await draft_limit(
        call,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        expires_at=expires_at,
        post_only=post_only,
    )
    if dry_run:
        return OrderResult(status="preview", dry_run=True, preview=draft.preview)
    print_ = safety.fingerprint("limit", token_id, side, str(price), str(size))
    await _before_send(call, draft.usd, print_)
    body = sign_draft(call, signer_of(call), draft)
    data, unknown = await safety.send_write(
        call,
        tool="trading:place_limit_order",
        endpoint=ep.CLOB_POST_ORDER,
        body=body,
        summary=f"{side} {size} @ {price} of {token_id}",
        usd=draft.usd,
        print_=print_,
        rejected=_rejected,
    )
    if unknown:
        return OrderResult(**unknown.model_dump(), preview=draft.preview)
    result = outcome_of(data)
    result.preview = draft.preview
    return result


@operation(
    "Place a market order",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    destructive=True,
    describe=_describe_market,
)
async def place_market_order(
    call: Call,
    token_id: TokenId,
    side: Side,
    amount: Annotated[
        Decimal, Field(gt=0, description="BUY: pUSD to spend. SELL: shares to sell.")
    ],
    price_bound: Annotated[
        Decimal,
        Field(gt=0, lt=1, description="BUY: highest price to pay. SELL: lowest price to accept."),
    ],
    order_type: Annotated[
        Literal["FAK", "FOK"],
        Field(description="FAK fills what it can now; FOK fills completely or not at all."),
    ] = "FAK",
    dry_run: DryRun = False,
) -> OrderResult:
    """Trade now against the book, never beyond `price_bound`. Use get_quotes first to pick a
    bound. Only place orders the user explicitly asked for."""
    from mcp_server_polymarket.signing.orders import check_price, market_amounts

    book = await book_of(call, token_id)
    tick = _decimal(book.tick_size) or Decimal("0.01")
    check_price(price_bound, tick, "price_bound")
    best = _decimal(book.best_ask if side == "BUY" else book.best_bid)
    if best is None:
        raise InvalidRequest(f"There is nothing to {'buy' if side == 'BUY' else 'sell into'}.")
    safety.check_deviation(
        price_bound,
        best,
        side,
        "price_bound",
        relative=False,
        limit=call.settings.max_price_deviation,
    )
    maker, taker = market_amounts(side, amount, price_bound, tick)
    usd = amount if side == "BUY" else amount * max(best, price_bound)
    question, outcome = await market_label(call, token_id)
    preview = OrderPreview(
        market=question,
        outcome=outcome,
        token_id=token_id,
        side=side,
        order_type=order_type,
        price=format(price_bound, "f"),
        size=None if side == "BUY" else format(amount, "f"),
        spend_usd=format(amount, "f") if side == "BUY" else None,
        notional_usd=format(usd.quantize(Decimal("0.01")), "f"),
        maker_amount=maker,
        taker_amount=taker,
        tick_size=format(tick, "f"),
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        neg_risk=book.neg_risk,
        protocol="v2" if is_v2_token(token_id) else "ctf",
    )
    if dry_run:
        return OrderResult(status="preview", dry_run=True, preview=preview)
    draft = Draft(
        token_id=token_id,
        side=side,
        order_type=order_type,
        maker_amount=maker,
        taker_amount=taker,
        neg_risk=bool(book.neg_risk),
        expiration=0,
        post_only=False,
        preview=preview,
        usd=usd,
    )
    print_ = safety.fingerprint("market", token_id, side, str(amount), str(price_bound))
    await _before_send(call, usd, print_)
    body = sign_draft(call, signer_of(call), draft)
    data, unknown = await safety.send_write(
        call,
        tool="trading:place_market_order",
        endpoint=ep.CLOB_POST_ORDER,
        body=body,
        summary=f"market {side} {amount} bound {price_bound} of {token_id}",
        usd=usd,
        print_=print_,
        rejected=_rejected,
    )
    if unknown:
        return OrderResult(**unknown.model_dump(), preview=preview)
    result = outcome_of(data)
    result.preview = preview
    return result


class LimitOrderSpec(BaseModel):
    token_id: str
    side: Side
    price: Price
    size: Shares
    post_only: bool = False


@operation(
    "Place several limit orders",
    toolset=TOOLSET,
    requires=TRADER,
    gate="trading",
    destructive=True,
    describe=_describe_batch,
)
async def place_orders(
    call: Call,
    orders: Annotated[list[LimitOrderSpec], Field(min_length=1, max_length=MAX_BATCH)],
    dry_run: DryRun = False,
) -> OrdersResult:
    """Up to 15 limit orders in one request, for example both sides of a market. Each is checked
    like place_limit_order; the limits apply to their total. Orders succeed or fail one by one."""
    drafts = [
        await draft_limit(
            call,
            token_id=spec.token_id,
            side=spec.side,
            price=spec.price,
            size=spec.size,
            expires_at=None,
            post_only=spec.post_only,
        )
        for spec in orders
    ]
    total = sum((d.usd for d in drafts), Decimal(0))
    previews = [OrderResult(status="preview", preview=d.preview) for d in drafts]
    if dry_run:
        return OrdersResult(
            status="preview",
            dry_run=True,
            orders=previews,
            message=f"Total ${total.quantize(Decimal('0.01'))}.",
        )
    print_ = safety.fingerprint(
        "batch", [(s.token_id, s.side, str(s.price), str(s.size)) for s in orders]
    )
    await _before_send(call, total, print_)
    signer = signer_of(call)
    body = [sign_draft(call, signer, draft) for draft in drafts]
    data, unknown = await safety.send_write(
        call,
        tool="trading:place_orders",
        endpoint=ep.CLOB_POST_ORDERS,
        body=body,
        summary=f"{len(body)} limit orders",
        usd=total,
        print_=print_,
        rejected=lambda d: all(_rejected(row) for row in (d if isinstance(d, list) else [d])),
    )
    if unknown:
        return OrdersResult(**unknown.model_dump(), orders=previews)
    rows = data if isinstance(data, list) else [data]
    results = []
    for draft, row in zip(drafts, rows, strict=False):
        result = outcome_of(row)
        result.preview = draft.preview
        results.append(result)
    placed = sum(1 for r in results if r.status == "placed")
    status = "placed" if placed == len(results) else "partial" if placed else "rejected"
    return OrdersResult(
        status=status, orders=results, message=f"{placed} of {len(results)} placed."
    )


class CancelResult(WriteResult):
    canceled: list[str] | None = None
    not_canceled: dict[str, Any] | None = None


def _cancel_result(data: Any) -> CancelResult:
    data = data if isinstance(data, dict) else {}
    canceled = data.get("canceled") or []
    failed = data.get("not_canceled") or {}
    status = "canceled" if canceled and not failed else "partial" if canceled else "none_canceled"
    return CancelResult(status=status, canceled=canceled, not_canceled=failed or None)


@operation(
    "Cancel orders",
    toolset=TOOLSET,
    requires=("clob_l2",),
    gate="cancel",
    idempotent=True,
)
async def cancel_orders(
    call: Call, order_ids: Annotated[list[str], Field(min_length=1, max_length=1000)]
) -> CancelResult:
    """Cancel resting orders by id. Safe to repeat: an order already gone is reported as not
    cancelled with the reason."""
    if len(order_ids) == 1:
        data = await call.json(ep.CLOB_CANCEL_ORDER, body={"orderID": order_ids[0]})
    else:
        data = await call.json(ep.CLOB_CANCEL_ORDERS, body=order_ids)
    return _cancel_result(data)


@operation(
    "Cancel all my orders",
    toolset=TOOLSET,
    requires=("clob_l2",),
    gate="cancel",
    idempotent=True,
    destructive=True,
)
async def cancel_all_orders(
    call: Call,
    condition_id: Annotated[str | None, Field(description="Only this market.")] = None,
    token_id: Annotated[str | None, Field(description="Only this outcome token.")] = None,
    wallet_wide: Annotated[
        bool,
        Field(
            description="Deposit Wallet owner: also orders placed by session keys and other "
            "API keys."
        ),
    ] = False,
) -> CancelResult:
    """Cancel every resting order of this API key, or only those in one market or token. With
    wallet_wide, every order on the Deposit Wallet whichever key placed it."""
    if wallet_wide:
        if condition_id or token_id:
            raise InvalidRequest("wallet_wide cancels everything; it takes no market filter.")
        data = await call.json(ep.CLOB_CANCEL_WALLET, body={"all": True})
    elif condition_id or token_id:
        body = {k: v for k, v in (("market", condition_id), ("asset_id", token_id)) if v}
        data = await call.json(ep.CLOB_CANCEL_MARKET, body=body)
    else:
        data = await call.json(ep.CLOB_CANCEL_ALL)
    return _cancel_result(data)


@operation("Mark notifications read", toolset=TOOLSET, requires=("clob_l2",), idempotent=True)
async def mark_notifications_read(
    call: Call, notification_ids: Annotated[list[str], Field(min_length=1)]
) -> WriteResult:
    """Mark CLOB notifications as read so they stop appearing in list_my_notifications."""
    await call.json(ep.CLOB_DROP_NOTIFICATIONS, params={"ids": ",".join(notification_ids)})
    return WriteResult(status="done")


class UnconfirmedWrite(CompactModel):
    request_id: str
    tool: str
    summary: str
    usd: str | None = None
    created: str
    resendable: bool


class UnconfirmedWrites(CompactModel):
    writes: list[UnconfirmedWrite]


@operation("List writes with an unknown outcome", toolset=TOOLSET, gate="cancel", read_only=True)
async def list_unconfirmed_writes(call: Call) -> UnconfirmedWrites:
    """Writes this server sent whose outcome is unknown because the connection failed after
    sending. Check the account before resending one."""
    from mcp_server_polymarket.models.base import iso_time

    return UnconfirmedWrites(
        writes=[
            UnconfirmedWrite(
                request_id=entry.id,
                tool=entry.tool.split(":", 1)[-1],
                summary=entry.summary,
                usd=entry.usd,
                created=iso_time(int(entry.created)) or "",
                resendable=entry.body is not None,
            )
            for entry in call.runtime.journal.unknown()
        ]
    )


async def _describe_resend(call: Call, request_id: str) -> str:
    entry = call.runtime.journal.get(request_id)
    if entry is None:
        raise NotFound(f"No write {request_id}.")
    return f"Send again exactly the same request as before: {entry.summary}. Resend it?"


@operation(
    "Resend a write with an unknown outcome",
    toolset=TOOLSET,
    gate="trading",
    destructive=True,
    describe=_describe_resend,
)
async def resend_write(call: Call, request_id: str) -> WriteResult:
    """Send the identical bytes of a write whose outcome is unknown. Polymarket rejects a
    duplicate, so this cannot place the order twice; a new tool call would sign a new one.
    Check the account first: if the write went through, there is nothing to do."""
    entry = call.runtime.journal.get(request_id)
    if entry is None:
        raise NotFound(f"No write {request_id}.")
    if entry.status != "unknown" or entry.body is None:
        raise Refused(
            f"Write {request_id} is {entry.status} and cannot be resent"
            + (" (its signed body has expired)." if entry.body is None else ".")
        )
    if entry.tool.startswith("transfers") and "transfers" not in call.runtime.caps.gates:
        raise Refused("Resending a transfer needs POLYMARKET_ALLOW_TRANSFERS=true.")
    endpoint = next((e for e in ep.ALL if e.key == entry.endpoint), None)
    if endpoint is None:
        raise Refused(f"Write {request_id} targets an endpoint this version does not know.")
    if not entry.tool.startswith("trading:mark"):
        await safety.check_geoblock(call)
    journal = call.runtime.journal
    try:
        response = await call.request(endpoint, raw_body=entry.body)
    except BaseException as exc:
        # The entry stays unknown: this attempt may have arrived as well.
        if isinstance(exc, Exception):
            raise Refused(
                f"Resending failed: {exc}", hint="Check the account before trying again."
            ) from exc
        raise
    from mcp_server_polymarket.client.base import parse_json

    data = parse_json(response)
    verdict = _resend_verdict(entry.tool, data)
    if verdict is not None:
        journal.finish(request_id, "rejected", data)
        return WriteResult(
            status="rejected",
            request_id=request_id,
            message=verdict,
            next_step="Check the account: a rejected duplicate can mean the first one arrived.",
            raw=data,
        )
    journal.finish(request_id, "sent", data)
    return WriteResult(status="sent", request_id=request_id, raw=data)


def _resend_verdict(tool: str, data: Any) -> str | None:
    """Polymarket's refusal of a resent request, when it comes back as a 200 response."""
    if tool.startswith(("perps", "transfers:withdraw", "transfers:transfer")):
        from mcp_server_polymarket.ops.perps_trading import _outcome

        result = _outcome(data)
    else:
        result = outcome_of(data)
    return (result.message or "rejected") if result.status == "rejected" else None
