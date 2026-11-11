import json

import pytest
from httpx2.websockets import ASGIWebSocketTransport
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from mcp_server_polymarket.server import create_server
from tests.mcp.helpers import call, error

pytestmark = pytest.mark.anyio
RECEIVED: list[str] = []


def book(asset: str) -> dict:
    return {
        "event_type": "book",
        "asset_id": asset,
        "bids": [{"price": str(p / 100), "size": "10"} for p in range(1, 20)],
        "asks": [{"price": str(p / 100), "size": "10"} for p in range(30, 50)],
    }


async def market(ws: WebSocket) -> None:
    await ws.accept()
    subscribe = json.loads(await ws.receive_text())
    RECEIVED.append(json.dumps(subscribe))
    await ws.send_text(json.dumps([book(a) for a in subscribe["assets_ids"]]))
    for i in range(10):
        await ws.send_text(json.dumps({"event_type": "price_change", "seq": i}))
    await ws.receive_text()


async def user(ws: WebSocket) -> None:
    await ws.accept()
    RECEIVED.append(await ws.receive_text())
    await ws.send_text(json.dumps({"event_type": "order", "type": "PLACEMENT", "id": "0x1"}))
    await ws.receive_text()


async def sports(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text("ping")
    RECEIVED.append(await ws.receive_text())
    for slug in ("nba-lal-bos", "nfl-kc-buf", "nba-lal-bos"):
        await ws.send_text(json.dumps({"type": "sportsUpdate", "slug": slug, "score": "1-0"}))
    await ws.close()


async def live(ws: WebSocket) -> None:
    await ws.accept()
    while True:
        message = json.loads(await ws.receive_text())
        RECEIVED.append(json.dumps(message))
        if message["op"] == "auth":
            await ws.send_text(json.dumps({"op": "authed", "rid": message["rid"]}))
            continue
        if message["filter"].get("symbol") == "nope":
            await ws.send_text(json.dumps({"op": "error", "code": "bad_filter"}))
            await ws.close(4000)
            return
        await ws.send_text(json.dumps({"op": "subscribed", "rid": message["rid"]}))
        for seq in range(3):
            frame = {"v": 1, "channel": message["channel"], "seq": seq, "payload": {"value": 1}}
            await ws.send_text(json.dumps(frame))
        await ws.receive_text()
        return


async def perps(ws: WebSocket) -> None:
    await ws.accept()
    while True:
        message = json.loads(await ws.receive_text())
        RECEIVED.append(json.dumps(message))
        if message.get("op", {}).get("type") == "auth":
            await ws.send_text(json.dumps({"id": message["id"], "data": {"status": "ok"}}))
            continue
        await ws.send_text(json.dumps({"id": message["id"], "data": [{"status": "ok"}]}))
        for channel in message["chs"]:
            frame = {"ch": channel, "ts": 1794435252089, "sq": 1, "data": {"price": "1"}}
            await ws.send_text(json.dumps(frame))
        await ws.send_text(json.dumps({"type": "resync"}))
        await ws.receive_text()
        return


APP = Starlette(
    routes=[
        WebSocketRoute("/ws/market", market),
        WebSocketRoute("/ws/user", user),
        WebSocketRoute("/sports", sports),
        WebSocketRoute("/live", live),
        WebSocketRoute("/perps", perps),
    ]
)


@pytest.fixture
def ws_settings(settings):
    settings.toolsets = "all"
    settings.clob_ws_url = "ws://localhost"
    settings.sports_ws_url = "ws://localhost/sports"
    settings.live_ws_url = "ws://localhost/live"
    settings.perps_ws_url = "ws://localhost/perps"
    RECEIVED.clear()
    return settings


@pytest.fixture
async def ws_transport():
    # The ASGI transport runs the app in its own task group, which must be entered first.
    transport = ASGIWebSocketTransport(APP)
    async with transport:
        yield transport


@pytest.fixture
def open_session(ws_transport):
    from mcp import Client

    async def make(settings, api):
        return Client(create_server(settings, transport=api.transport, ws_transport=ws_transport))

    return make


async def test_watch_market_subscribes_and_trims_books(ws_settings, api, open_session):
    async with await open_session(ws_settings, api) as client:
        data = await call(client, "watch_market", token_ids=["7"], max_events=4, seconds=5)
    subscribe = json.loads(RECEIVED[0])
    assert subscribe == {
        "type": "market",
        "assets_ids": ["7"],
        "initial_dump": True,
        "custom_feature_enabled": True,
    }
    assert data["counts"] == {"book": 1, "price_change": 3}
    first = data["events"][0]
    assert [level["price"] for level in first["bids"]][:2] == ["0.19", "0.18"]
    assert len(first["asks"]) == 5 and first["asks_levels"] == 20
    assert data["truncated"] is True


async def test_watch_my_orders_needs_and_sends_credentials(ws_settings, api, open_session):
    ws_settings.api_key = "key-123"
    ws_settings.api_secret = "c2VjcmV0"
    ws_settings.api_passphrase = "pass-456"
    ws_settings.address = "0x" + "1" * 40
    async with await open_session(ws_settings, api) as client:
        data = await call(client, "watch_my_orders", max_events=1, seconds=5)
    subscribe = json.loads(RECEIVED[0])
    assert subscribe["auth"] == {"apiKey": "key-123", "secret": "c2VjcmV0", "passphrase": "pass-456"}
    assert data["events"][0]["type"] == "PLACEMENT"


async def test_watch_sports_answers_pings_and_filters(ws_settings, api, open_session):
    async with await open_session(ws_settings, api) as client:
        data = await call(client, "watch_sports", slug="nba", max_events=2, seconds=5)
    assert RECEIVED == ["pong"]
    assert [e["slug"] for e in data["events"]] == ["nba-lal-bos", "nba-lal-bos"]


async def test_watch_live_data_public_and_authenticated(ws_settings, api, open_session):
    async with await open_session(ws_settings, api) as client:
        data = await call(
            client, "watch_live_data", channel="price.polymarket", token_id="5", max_events=3
        )
        assert data["counts"] == {"price.polymarket": 3}
        assert data["acknowledgements"][0]["op"] == "subscribed"
        assert "needs CLOB API credentials" in await error(
            client, "watch_live_data", channel="price.crypto", symbol="btcusd"
        )
    ws_settings.api_key, ws_settings.api_secret = "k", "c2VjcmV0"
    ws_settings.api_passphrase, ws_settings.address = "p", "0x" + "1" * 40
    RECEIVED.clear()
    async with await open_session(ws_settings, api) as client:
        await call(
            client,
            "watch_live_data",
            channel="price.crypto.twap",
            symbol="BTCUSD",
            provider="chainlink",
            max_events=3,
        )
        assert "refused" in await error(
            client, "watch_live_data", channel="price.crypto", symbol="nope", seconds=2
        )
    auth, subscribe = (json.loads(m) for m in RECEIVED[:2])
    assert auth["op"] == "auth" and auth["auth"]["apiKey"] == "k"
    assert subscribe["filter"] == {"symbol": "btcusd", "window_seconds": 60, "provider": "chainlink"}


async def test_watch_perps_market_and_account(ws_settings, api, open_session):
    async with await open_session(ws_settings, api) as client:
        data = await call(
            client,
            "watch_perps_market",
            instrument_id=6,
            channels=["trades", "klines", "book"],
            kline_interval="5m",
            book_depth=20,
            max_events=4,
        )
    assert json.loads(RECEIVED[0])["chs"] == ["trades::6", "klines::6::5m", "book::6::20"]
    assert data["counts"]["trades::6"] == 1
    assert data["events"][0]["ts"].endswith("Z")
    assert "resync" in data["note"]
    ws_settings.perps_proxy, ws_settings.perps_secret = "0x" + "2" * 40, "perps-secret"
    RECEIVED.clear()
    async with await open_session(ws_settings, api) as client:
        await call(client, "watch_my_perps", channels=["fills"], max_events=1)
    auth = json.loads(RECEIVED[0])
    assert auth["op"]["args"] == {"proxy": "0x" + "2" * 40, "secret": "perps-secret"}
