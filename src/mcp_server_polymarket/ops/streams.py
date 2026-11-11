"""Watch tools: a short WebSocket capture, summarised."""

from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client.ws import Capture, Heartbeat, collect
from mcp_server_polymarket.errors import AuthError, InvalidRequest
from mcp_server_polymarket.models.base import CompactModel
from mcp_server_polymarket.ops.perps_market import perps_slim
from mcp_server_polymarket.ops.registry import Call, operation

Seconds = Annotated[float, Field(ge=1, le=60, description="How long to listen.")]
MaxEvents = Annotated[int, Field(ge=1, le=500, description="Stop after this many messages.")]
_LEVELS = 5


class StreamCapture(CompactModel):
    """What arrived during the capture. counts is per message type."""

    source: str
    seconds: float
    counts: dict[str, int]
    events: list[dict[str, Any]]
    acknowledgements: list[Any] | None = None
    truncated: bool | None = None
    closed: str | None = None
    note: str | None = None


def _trim_book(event: dict[str, Any]) -> dict[str, Any]:
    trimmed = dict(event)
    for side, best_high in (("bids", True), ("asks", False)):
        levels = event.get(side)
        if isinstance(levels, list):
            ordered = sorted(
                (lv for lv in levels if isinstance(lv, dict)),
                key=lambda lv: float(lv.get("price", 0)),
                reverse=best_high,
            )
            trimmed[side] = ordered[:_LEVELS]
            trimmed[f"{side}_levels"] = len(levels)
    return trimmed


def _summarise(source: str, capture: Capture, kind: str, trim: Any = None) -> StreamCapture:
    events = []
    counts: Counter[str] = Counter()
    for event in capture.events:
        if not isinstance(event, dict):
            counts["text"] += 1
            continue
        counts[str(event.get(kind) or "message")] += 1
        events.append(trim(event) if trim else event)
    note = None
    if not capture.events:
        note = "Nothing arrived. The market may be quiet; try a longer capture."
    return StreamCapture(
        source=source,
        seconds=capture.seconds,
        counts=dict(counts),
        events=events,
        acknowledgements=capture.replies or None,
        truncated=capture.truncated or None,
        closed=capture.closed,
        note=note,
    )


def _clob_auth(call: Call) -> dict[str, str]:
    credentials = call.identity.clob_credentials()
    if credentials is None:
        raise AuthError(
            "This stream needs CLOB API credentials.",
            hint="Run `mcp-server-polymarket auth`, or set POLYMARKET_API_KEY and friends.",
        )
    return {
        "apiKey": credentials.api_key,
        "secret": credentials.secret,
        "passphrase": credentials.passphrase,
    }


@operation("Watch order books live", toolset="streams", read_only=True)
async def watch_market(
    call: Call,
    token_ids: Annotated[list[str], Field(min_length=1, max_length=50)],
    seconds: Seconds = 10,
    max_events: MaxEvents = 50,
    initial_book: Annotated[bool, Field(description="Start with a full book per token.")] = True,
    extra_events: Annotated[
        bool, Field(description="Also best_bid_ask, new_market and market_resolved events.")
    ] = True,
) -> StreamCapture:
    """Listen to the CLOB market channel for a few seconds: book snapshots, price changes, trades
    (last_trade_price) and tick size changes for these tokens. Books show the best 5 levels."""
    capture = await collect(
        call.client.ws,
        f"{call.settings.clob_ws_url}/ws/market",
        send=[
            {
                "type": "market",
                "assets_ids": token_ids,
                "initial_dump": initial_book,
                "custom_feature_enabled": extra_events,
            }
        ],
        seconds=call.limit_wait(seconds),
        max_events=max_events,
        heartbeat=Heartbeat(every=9, message="PING"),
    )

    def trim(event: dict[str, Any]) -> dict[str, Any]:
        return _trim_book(event) if event.get("event_type") == "book" else event

    return _summarise("clob market channel", capture, "event_type", trim)


@operation(
    "Watch my orders and trades live",
    toolset="account",
    requires=("clob_l2",),
    read_only=True,
)
async def watch_my_orders(
    call: Call,
    condition_ids: Annotated[
        list[str] | None, Field(description="Only these markets. Empty means all.")
    ] = None,
    seconds: Seconds = 15,
    max_events: MaxEvents = 50,
) -> StreamCapture:
    """Listen to the authenticated CLOB user channel: order placements, updates and
    cancellations, and trades as they move from MATCHED to CONFIRMED. Nothing is replayed, so
    read list_my_open_orders first for the current state."""
    subscribe = {"type": "user", "auth": _clob_auth(call), "markets": condition_ids or []}
    capture = await collect(
        call.client.ws,
        f"{call.settings.clob_ws_url}/ws/user",
        send=[subscribe],
        seconds=call.limit_wait(seconds),
        max_events=max_events,
        heartbeat=Heartbeat(every=9, message="PING"),
    )
    return _summarise("clob user channel", capture, "event_type")


@operation("Watch live sports scores", toolset="sports", read_only=True)
async def watch_sports(
    call: Call,
    slug: Annotated[
        str | None, Field(description="Only this game's updates (substring of its slug).")
    ] = None,
    seconds: Seconds = 10,
    max_events: MaxEvents = 50,
) -> StreamCapture:
    """Listen to the sports channel: score, period and status updates for games in play."""
    capture = await collect(
        call.client.ws,
        call.settings.sports_ws_url,
        send=[],
        seconds=call.limit_wait(seconds),
        max_events=max_events * 20 if slug else max_events,
        reply=lambda text: "pong" if text == "ping" else None,
    )
    if slug:
        capture.events = [
            e for e in capture.events if isinstance(e, dict) and slug in str(e.get("slug", ""))
        ][:max_events]
    return _summarise("sports channel", capture, "type")


LiveChannel = Literal[
    "price.polymarket",
    "price.crypto",
    "price.equity",
    "price.crypto.twap",
    "price.equity.twap",
    "esports.series",
]
_PUBLIC_LIVE = {"price.polymarket", "esports.series"}


@operation("Watch reference prices live", toolset="streams", read_only=True)
async def watch_live_data(
    call: Call,
    channel: LiveChannel,
    symbol: Annotated[
        str | None, Field(description="price.crypto/equity: e.g. btcusd, aapl, eurusd.")
    ] = None,
    token_id: Annotated[str | None, Field(description="price.polymarket: a token id.")] = None,
    event_id: Annotated[str | None, Field(description="esports.series: an event id.")] = None,
    provider: Annotated[
        Literal["chainlink", "pyth"] | None, Field(description="Pin a price source.")
    ] = None,
    seconds: Seconds = 10,
    max_events: MaxEvents = 50,
) -> StreamCapture:
    """Listen to PolyBolt: the crypto, equity and TWAP reference prices that crypto and stock
    markets resolve on, best bid/ask of a Polymarket token, or esports series state. Starts
    with a snapshot of the last two minutes. Price channels other than price.polymarket need
    CLOB API credentials."""
    filters: dict[str, Any] = {}
    if channel == "price.polymarket":
        if not token_id:
            raise InvalidRequest("price.polymarket needs token_id.")
        filters["asset_id"] = token_id
    elif channel == "esports.series":
        if not event_id:
            raise InvalidRequest("esports.series needs event_id.")
        filters["event_id"] = event_id
    else:
        if not symbol:
            raise InvalidRequest(f"{channel} needs symbol, e.g. btcusd.")
        filters["symbol"] = symbol.lower()
        if channel.endswith(".twap"):
            filters["window_seconds"] = 60
    if provider:
        filters["provider"] = provider
    send: list[Any] = []
    if channel not in _PUBLIC_LIVE:
        send.append({"op": "auth", "rid": "auth", "auth": _clob_auth(call)})
    send.append({"op": "subscribe", "rid": "sub", "channel": channel, "filter": filters})
    capture = await collect(
        call.client.ws,
        call.settings.live_ws_url,
        send=send,
        seconds=call.limit_wait(seconds),
        max_events=max_events,
        is_reply=lambda m: isinstance(m, dict) and "op" in m and "payload" not in m,
    )
    errors = [r for r in capture.replies if isinstance(r, dict) and r.get("op") == "error"]
    if errors and not capture.events:
        raise InvalidRequest(f"PolyBolt refused the subscription: {errors[0]}")
    return _summarise("polybolt", capture, "channel")


PerpsChannel = Literal["trades", "bbo", "book", "klines", "tickers", "statistics"]
PrivatePerpsChannel = Literal[
    "orders",
    "fills",
    "portfolio",
    "balances",
    "funding",
    "deposits",
    "withdrawals",
    "notifications",
    "tpsl",
    "backstop",
    "builderFills",
]


def _perps_reply(message: Any) -> bool:
    # Answers to our requests carry the request id; channel updates and resyncs do not.
    return isinstance(message, dict) and "id" in message and "ch" not in message


def _perps_capture(source: str, capture: Capture) -> StreamCapture:
    capture.events = [perps_slim(e) for e in capture.events]
    resync = [e for e in capture.events if isinstance(e, dict) and e.get("type") == "resync"]
    summary = _summarise(source, capture, "ch")
    if resync:
        summary.note = "The server dropped data (resync); reconcile with the REST tools."
    return summary


@operation("Watch Perps markets live", toolset="streams", read_only=True)
async def watch_perps_market(
    call: Call,
    instrument_id: Annotated[int, Field(ge=1)],
    channels: Annotated[list[PerpsChannel], Field(min_length=1)] = ["trades", "bbo"],  # noqa: B006
    kline_interval: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1m",
    book_depth: Literal[20, 50] | None = None,
    seconds: Seconds = 10,
    max_events: MaxEvents = 50,
) -> StreamCapture:
    """Listen to public Perps channels for one instrument: trades, best bid/offer, book,
    candles, ticker or 24h statistics."""
    names = []
    for channel in channels:
        if channel == "klines":
            names.append(f"klines::{instrument_id}::{kline_interval}")
        elif channel == "book" and book_depth:
            names.append(f"book::{instrument_id}::{book_depth}")
        else:
            names.append(f"{channel}::{instrument_id}")
    capture = await collect(
        call.client.ws,
        call.settings.perps_ws_url,
        send=[{"id": 1, "req": "sub", "chs": names}],
        seconds=call.limit_wait(seconds),
        max_events=max_events,
        heartbeat=Heartbeat(every=25, message='{"req":"post","op":{"type":"ping"}}'),
        is_reply=_perps_reply,
    )
    return _perps_capture("perps public channels", capture)


@operation(
    "Watch my Perps account live",
    toolset="perps-account",
    requires=("perps_session",),
    read_only=True,
)
async def watch_my_perps(
    call: Call,
    channels: Annotated[list[PrivatePerpsChannel], Field(min_length=1)] = [  # noqa: B006
        "orders",
        "fills",
        "portfolio",
    ],
    seconds: Seconds = 15,
    max_events: MaxEvents = 50,
) -> StreamCapture:
    """Listen to private Perps channels: order and fill updates, portfolio, balances, funding,
    deposits, withdrawals, notifications and TP/SL lifecycle."""
    session = call.identity.perps_session()
    if session is None:
        raise AuthError("No Perps session.", hint="Run `mcp-server-polymarket auth --perps`.")
    auth = {
        "id": 1,
        "req": "post",
        "op": {"type": "auth", "args": {"proxy": session.proxy, "secret": session.secret}},
    }
    capture = await collect(
        call.client.ws,
        call.settings.perps_ws_url,
        send=[auth, {"id": 2, "req": "sub", "chs": list(channels)}],
        seconds=call.limit_wait(seconds),
        max_events=max_events,
        heartbeat=Heartbeat(every=25, message='{"req":"post","op":{"type":"ping"}}'),
        is_reply=_perps_reply,
    )
    return _perps_capture("perps private channels", capture)
