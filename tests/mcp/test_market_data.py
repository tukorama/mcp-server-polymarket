import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio

BOOK = fixture("clob_book")


async def test_order_book_is_sorted_best_first(settings, api):
    api.add(ep.CLOB_BOOK, BOOK)
    async with session(settings, api) as client:
        data = await call(client, "get_order_books", token_ids=[BOOK["asset_id"]], depth=2)
    book = data["books"][0]
    bids = [level["price"] for level in book["bids"]]
    asks = [level["price"] for level in book["asks"]]
    assert bids == sorted(bids, reverse=True) and asks == sorted(asks)
    assert book["best_bid"] == bids[0] and book["best_ask"] == asks[0]
    assert book["spread"] == pytest.approx(book["best_ask"] - book["best_bid"])
    assert len(book["bids"]) == 2 and book["tick_size"] == 0.01


async def test_quotes_for_many_tokens_use_one_request(settings, api):
    books = fixture("clob_books")
    api.add(ep.CLOB_BOOKS, books)
    async with session(settings, api) as client:
        data = await call(client, "get_quotes", token_ids=[b["asset_id"] for b in books])
    assert len(api.calls(ep.CLOB_BOOKS)) == 1
    assert api.body(ep.CLOB_BOOKS) == [{"token_id": b["asset_id"]} for b in books]
    assert {q["token_id"] for q in data["quotes"]} == {b["asset_id"] for b in books}
    assert all("mid" in q for q in data["quotes"] if "best_bid" in q and "best_ask" in q)


async def test_missing_book_is_not_found(settings, api):
    api.add(ep.CLOB_BOOK, (404, {"error": "No orderbook exists for the requested token id"}))
    async with session(settings, api) as client:
        assert "No orderbook" in await error(client, "get_order_books", token_ids=["1"])


async def test_price_history_summarises(settings, api):
    api.add(ep.DATA_PRICES_HISTORY, fixture("data_prices_history"))
    async with session(settings, api) as client:
        data = await call(client, "get_price_history", token_id="9", interval="1w")
        assert api.query(ep.DATA_PRICES_HISTORY) == {
            "token_id": "9",
            "limit": "500",
            "interval": "1w",
        }
        assert data["points"][0]["time"].endswith("Z")
        assert data["resolution_seconds"] == 300
        assert data["high"] >= data["low"]
        await call(
            client, "get_price_history", token_id="9", start="2026-11-01", bucket_seconds=3600
        )
        query = api.query(ep.DATA_PRICES_HISTORY)
        assert query["start"] == "1793491200" and query["bucket_seconds"] == "3600"
        await call(client, "get_price_history", token_id="9", as_of="1793491200", bucket_seconds=60)
        assert "bucket_seconds" not in api.query(ep.DATA_PRICES_HISTORY)


async def test_trading_info_resolves_tokens(settings, api):
    api.add(ep.CLOB_MARKET_BY_TOKEN, fixture("clob_market_by_token"))
    api.add(ep.CLOB_MARKET_INFO, fixture("clob_market_info"))
    async with session(settings, api) as client:
        data = await call(client, "get_market_trading_info", token_id="6013")
    assert data["tick_size"] == 0.01 and data["min_order_size"] == 5
    assert data["fee"] == {"rate": 0.05, "exponent": 1, "taker_only": True}
    assert [t["outcome"] for t in data["tokens"]] == ["Elina Svitolina", "Karolina Muchova"]


async def test_order_book_markets(settings, api):
    page = {
        "data": [
            {
                "condition_id": "0xa",
                "tokens": [{"token_id": "1", "outcome": "Yes", "price": 0.2, "winner": False}],
                "rewards": {"min_size": 200, "max_spread": 3.5},
                "active": True,
                "closed": False,
            }
        ],
        "next_cursor": "MTAw",
    }
    api.add(ep.CLOB_SIMPLIFIED_MARKETS, {**page, "next_cursor": "LTE="})
    api.add(ep.CLOB_SAMPLING_SIMPLIFIED_MARKETS, page)
    api.add(ep.CLOB_SAMPLING_MARKETS, page)
    async with session(settings, api) as client:
        data = await call(client, "list_order_book_markets")
        assert "next_cursor" not in data
        data = await call(client, "list_order_book_markets", sampling=True, cursor="MA==")
        assert data["next_cursor"] == "MTAw"
        assert api.query(ep.CLOB_SAMPLING_SIMPLIFIED_MARKETS) == {"next_cursor": "MA=="}
        await call(client, "list_order_book_markets", sampling=True, simplified=False)
        assert data["items"][0]["rewards_min_size"] == 200


async def test_live_activity(settings, api):
    one = fixture("clob_live_activity")
    api.add(ep.CLOB_LIVE_ACTIVITY_ONE, one)
    api.add(ep.CLOB_LIVE_ACTIVITY, [one, one])
    async with session(settings, api) as client:
        data = await call(client, "get_live_activity", condition_ids=[one["condition_id"]])
        assert data["markets"][0]["series_slug"] == "wta"
        await call(client, "get_live_activity", condition_ids=["0xa", "0xb"])
    assert api.body(ep.CLOB_LIVE_ACTIVITY) == ["0xa", "0xb"]
