"""Read-only checks against the real APIs. Run with `POLYMARKET_LIVE=1 uv run pytest -m live`.

Nothing here signs, places, cancels or moves anything, and no credentials are used. Test names
carry the service, so `-k "gamma or perps"` limits a run to hosts your network reaches.
"""

import os

import pytest
from mcp import Client

from mcp_server_polymarket.server import create_server
from tests.mcp.helpers import call

# Read at import: the autouse fixture clears POLYMARKET_* before each test.
LIVE = os.environ.get("POLYMARKET_LIVE") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(not LIVE, reason="set POLYMARKET_LIVE=1 to call the real APIs"),
]


@pytest.fixture
async def client(settings):
    settings.toolsets = "all"
    async with Client(create_server(settings)) as session:
        yield session


@pytest.fixture
async def market(client):
    page = await call(client, "list_markets", limit=5, order="volume_24h")
    assert page["items"], "Gamma returned no markets"
    return page["items"][0]


async def test_gamma_search_and_market(client, market):
    found = await call(client, "search", query="bitcoin", limit=3)
    assert found["events"] or found.get("markets")
    detail = await call(client, "get_market", slug=market["slug"])
    assert detail["outcomes"] and all(o.get("token_id") for o in detail["outcomes"])


async def test_gamma_events_tags_and_series(client):
    events = await call(client, "list_events", limit=3)
    assert events["items"]
    assert (await call(client, "get_event", slug=events["items"][0]["slug"]))["markets"]
    assert (await call(client, "list_tags", limit=5))["items"]


async def test_clob_quotes_and_books(client, market):
    token = market["outcomes"][0]["token_id"]
    quotes = await call(client, "get_quotes", token_ids=[token])
    assert quotes["quotes"][0]["tick_size"]
    books = await call(client, "get_order_books", token_ids=[token])
    assert books["books"][0]["token_id"] == token


async def test_data_positions_and_holders(client, market):
    holders = await call(client, "list_market_holders", condition_id=market["condition_id"])
    assert "tokens" in holders
    history = await call(
        client, "get_price_history", token_id=market["outcomes"][0]["token_id"], interval="1w"
    )
    assert history["points"]


async def test_perps_market_data(client):
    instruments = (await call(client, "list_perps_instruments"))["instruments"]
    assert instruments
    first = instruments[0]["instrument_id"]
    tickers = await call(client, "get_perps_tickers", instrument_ids=[first])
    assert tickers["tickers"][0]["instrument_id"] == first
    book = await call(client, "get_perps_book", instrument_id=first)
    assert book["bids"] or book["asks"]
    assert (await call(client, "get_perps_candles", instrument_id=first, interval="1h"))["candles"]


async def test_docs_search_and_read(client):
    results = (await call(client, "search_docs", query="order types"))["results"]
    assert results
    page = await call(client, "read_doc", url=results[0]["url"])
    assert len(page["text"]) > 200


async def test_status_and_geoblock(client):
    status = await call(client, "get_service_status")
    assert status["services"]
