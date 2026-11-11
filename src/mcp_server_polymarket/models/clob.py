from decimal import Decimal, InvalidOperation
from typing import Any

from mcp_server_polymarket.models.base import CompactModel, as_dict, as_float, iso_time
from mcp_server_polymarket.redaction import clean_text


class Level(CompactModel):
    price: float
    size: float


def _levels(raw: Any, *, bids: bool) -> list[Level]:
    levels = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        price, size = as_float(entry.get("price")), as_float(entry.get("size"))
        if price is not None and size is not None:
            levels.append(Level(price=price, size=size))
    # The API's own order is not reliable across versions, so sort: best price first.
    levels.sort(key=lambda level: level.price, reverse=bids)
    return levels


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


class OrderBook(CompactModel):
    """An outcome token's order book, best levels first. Prices are probabilities in pUSD."""

    token_id: str
    condition_id: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    last_trade_price: float | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    protocol: str | None = None
    bid_levels: int | None = None
    ask_levels: int | None = None
    bids: list[Level] = []
    asks: list[Level] = []
    timestamp: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, depth: int | None = None) -> "OrderBook":
        bids = _levels(raw.get("bids"), bids=True)
        asks = _levels(raw.get("asks"), bids=False)
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        mid = spread = None
        if best_bid is not None and best_ask is not None:
            bid, ask = Decimal(str(best_bid)), Decimal(str(best_ask))
            mid = float((bid + ask) / 2)
            spread = float(ask - bid)
        return cls(
            token_id=str(raw.get("asset_id")),
            condition_id=raw.get("market"),
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            last_trade_price=as_float(raw.get("last_trade_price")),
            tick_size=as_float(raw.get("tick_size")),
            min_order_size=as_float(raw.get("min_order_size")),
            neg_risk=raw.get("neg_risk"),
            protocol=raw.get("version"),
            bid_levels=len(bids),
            ask_levels=len(asks),
            bids=bids if depth is None else bids[:depth],
            asks=asks if depth is None else asks[:depth],
            timestamp=iso_time(raw.get("timestamp")),
        )


class Quote(CompactModel):
    """Top of book for one token, plus what an order there must respect."""

    token_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last_trade_price: float | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None

    @classmethod
    def from_book(cls, book: OrderBook) -> "Quote":
        return cls(
            token_id=book.token_id,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            mid=book.mid,
            spread=book.spread,
            bid_size=book.bids[0].size if book.bids else None,
            ask_size=book.asks[0].size if book.asks else None,
            last_trade_price=book.last_trade_price,
            tick_size=book.tick_size,
            min_order_size=book.min_order_size,
            neg_risk=book.neg_risk,
        )


class TokenOutcome(CompactModel):
    token_id: str
    outcome: str | None = None
    price: float | None = None
    winner: bool | None = None


class FeeDetails(CompactModel):
    rate: float | None = None
    exponent: float | None = None
    taker_only: bool | None = None


class TradingInfo(CompactModel):
    """CLOB parameters an order in this market has to respect."""

    condition_id: str
    tokens: list[TokenOutcome] = []
    accepting_orders: bool | None = None
    accepting_orders_since: str | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    maker_base_fee_bps: int | None = None
    taker_base_fee_bps: int | None = None
    fee: FeeDetails | None = None
    rewards_min_size: float | None = None
    rewards_max_spread: float | None = None
    rfq_enabled: bool | None = None
    game_start_time: str | None = None
    protocol: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "TradingInfo":
        rewards = as_dict(raw.get("r"))
        fee = raw.get("fd") if isinstance(raw.get("fd"), dict) else None
        return cls(
            condition_id=str(raw.get("c")),
            tokens=[
                TokenOutcome(token_id=str(t.get("t")), outcome=t.get("o"))
                for t in raw.get("t") or []
                if isinstance(t, dict)
            ],
            accepting_orders=raw.get("ao"),
            accepting_orders_since=iso_time(raw.get("aot")),
            tick_size=as_float(raw.get("mts")),
            min_order_size=as_float(raw.get("mos")),
            neg_risk=raw.get("nr"),
            maker_base_fee_bps=raw.get("mbf"),
            taker_base_fee_bps=raw.get("tbf"),
            fee=FeeDetails(rate=fee.get("r"), exponent=fee.get("e"), taker_only=fee.get("to"))
            if fee
            else None,
            rewards_min_size=as_float(rewards.get("mi")),
            rewards_max_spread=as_float(rewards.get("ma")),
            rfq_enabled=raw.get("rfqe") if "rfqe" in raw else None,
            game_start_time=iso_time(raw.get("gst")),
            protocol=raw.get("v"),
        )


class OrderBookMarket(CompactModel):
    condition_id: str
    question: str | None = None
    slug: str | None = None
    tokens: list[TokenOutcome] = []
    active: bool | None = None
    closed: bool | None = None
    accepting_orders: bool | None = None
    neg_risk: bool | None = None
    tick_size: float | None = None
    end_date: str | None = None
    rewards_min_size: float | None = None
    rewards_max_spread: float | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "OrderBookMarket":
        rewards = as_dict(raw.get("rewards"))
        return cls(
            condition_id=str(raw.get("condition_id")),
            question=clean_text(raw.get("question")),
            slug=raw.get("market_slug"),
            tokens=[
                TokenOutcome(
                    token_id=str(t.get("token_id")),
                    outcome=t.get("outcome"),
                    price=as_float(t.get("price")),
                    winner=t.get("winner") or None,
                )
                for t in raw.get("tokens") or []
                if isinstance(t, dict)
            ],
            active=raw.get("active"),
            closed=raw.get("closed"),
            accepting_orders=raw.get("accepting_orders"),
            neg_risk=raw.get("neg_risk"),
            tick_size=as_float(raw.get("minimum_tick_size")),
            end_date=iso_time(raw.get("end_date_iso")),
            rewards_min_size=as_float(rewards.get("min_size")) or None,
            rewards_max_spread=as_float(rewards.get("max_spread")) or None,
        )


class LiveActivity(CompactModel):
    condition_id: str
    market_id: str | None = None
    question: str | None = None
    market_slug: str | None = None
    event_slug: str | None = None
    series_slug: str | None = None
    tags: list[str] | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LiveActivity":
        return cls(
            condition_id=str(raw.get("condition_id")),
            market_id=str(raw["id"]) if raw.get("id") is not None else None,
            question=clean_text(raw.get("question")),
            market_slug=raw.get("market_slug"),
            event_slug=raw.get("event_slug"),
            series_slug=raw.get("series_slug"),
            tags=raw.get("tags") or None,
        )


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0
