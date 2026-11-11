import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio
ADDRESS = "0x5Eee21F22ac3Bb72c979D801270d7f322D6B2d6f"


async def test_exchange_info_combines_four_reads(settings, api):
    api.add(ep.PERPS_EXCHANGE, fixture("perps_exchange"))
    api.add(ep.PERPS_ASSETS, fixture("perps_assets"))
    api.add(ep.PERPS_FEES, fixture("perps_fees"))
    api.add(ep.PERPS_LIMIT_TIERS, (500, {"status": "err", "error": "internal_error"}))
    async with session(settings, api) as client:
        data = await call(client, "get_perps_exchange_info")
    assert data["exchange"]["chain_id"] == 137
    assert data["collateral_assets"][0]["asset"] == "pUSD"
    assert data["fee_schedule"][0]["taker_fee_rate"]
    assert data["errors"][0]["section"] == "limit_tiers"


async def test_instruments_are_cached_and_filtered(settings, api):
    api.add(ep.PERPS_INSTRUMENTS, fixture("perps_instruments"))
    async with session(settings, api) as client:
        data = await call(client, "list_perps_instruments", search="sp500")
        assert [i["symbol"] for i in data["instruments"]] == ["SP500-USD"]
        assert "risk_tiers" not in data["instruments"][0]
        assert "logo" not in data["instruments"][0]
        await call(client, "list_perps_instruments", category="index")
        assert len(api.calls(ep.PERPS_INSTRUMENTS)) == 1
        await call(client, "list_perps_instruments", instrument_id=1, include_risk_tiers=True)
        assert api.query(ep.PERPS_INSTRUMENTS) == {"instrument_id": "1"}


async def test_tickers_merge_statistics_and_bbo(settings, api):
    api.add(ep.PERPS_TICKERS, fixture("perps_tickers"))
    api.add(ep.PERPS_STATISTICS, fixture("perps_statistics"))
    api.add(ep.PERPS_BBO, fixture("perps_bbo"))
    async with session(settings, api) as client:
        data = await call(client, "get_perps_tickers", instrument_id=1, include_bbo=True)
    ticker = data["tickers"][0]
    assert ticker["mark_price"] and ticker["next_funding"].endswith("Z")
    assert "klines" not in ticker
    assert any(key.startswith("bbo_") or key in ("bid_price", "best_bid") for key in ticker) or ticker


async def test_book_and_candles(settings, api, clock):
    api.add(ep.PERPS_BOOK, fixture("perps_book"))
    api.add(ep.PERPS_KLINES, fixture("perps_klines"))
    api.add(ep.PERPS_MARK_HISTORY, fixture("perps_mark"))
    async with session(settings, api) as client:
        book = await call(client, "get_perps_book", instrument_id=1, depth=10)
        assert float(book["best_bid"]) < float(book["best_ask"])
        data = await call(client, "get_perps_candles", instrument_id=1, interval="1d", count=5)
        candle = data["candles"][0]
        assert {"open", "high", "low", "close", "volume"} <= set(candle)
        query = api.query(ep.PERPS_KLINES)
        assert query["interval"] == "1d" and int(query["start_timestamp"]) > 0
        data = await call(client, "get_perps_candles", instrument_id=1, kind="mark", start="2026-11-01")
        assert api.query(ep.PERPS_MARK_HISTORY)["start_timestamp"] == "1793491200000"
        assert set(data["candles"][0]) == {"time", "close"}


async def test_public_lists(settings, api):
    api.add(ep.PERPS_TRADES, fixture("perps_trades"))
    api.add(ep.PERPS_FUNDING_HISTORY, fixture("perps_funding"))
    api.add(ep.PERPS_INDEX, fixture("perps_index"))
    api.add(ep.PERPS_EXCHANGE_STATS, fixture("perps_exchange_stats"))
    api.add(ep.PERPS_INVITE_CHECK, fixture("perps_invite"))
    async with session(settings, api) as client:
        trades = await call(client, "list_perps_trades", instrument_id=1, start="2026-11-11")
        assert trades["items"][0]["timestamp"].endswith("Z") and trades["next_cursor"]
        funding = await call(client, "get_perps_funding_history", instrument_id=1)
        assert funding["items"][0]["funding_rate"]
        assert (await call(client, "get_perps_index", asset="SP500"))["index_price"]
        stats = await call(client, "get_perps_exchange_stats")
        assert stats["volume"]
        query = api.query(ep.PERPS_EXCHANGE_STATS)
        assert int(query["end_timestamp"]) - int(query["start_timestamp"]) == 86_400_000
        assert (await call(client, "check_perps_invite_code", code="X"))["valid"] is False


async def test_public_account(settings, api):
    api.add(ep.PERPS_REGISTERED, {"registered": True})
    api.add(ep.PERPS_PUBLIC_PORTFOLIO, fixture("perps_portfolio_public"))
    api.add(ep.PERPS_BUILDER_INFO, fixture("perps_builder"))
    api.add(ep.PERPS_PUBLIC_FILLS, fixture("perps_public_fills"))
    api.add(ep.PERPS_POSITION_FILLS, fixture("perps_public_fills"))
    async with session(settings, api) as client:
        data = await call(client, "get_perps_public_account", address=ADDRESS, include_builder=True)
        assert data["registered"] is True and data["portfolio"]["positions"]
        assert "errors" not in data
        fills = await call(client, "list_perps_public_fills", address=ADDRESS, oldest_first=True)
        assert api.query(ep.PERPS_PUBLIC_FILLS)["sort"] == "asc" and fills["items"]
        await call(
            client, "list_perps_public_fills", address=ADDRESS, instrument_id=1, current_position=True
        )
        assert "needs instrument_id" in await error(
            client, "list_perps_public_fills", address=ADDRESS, current_position=True
        )


async def test_unregistered_account_is_not_an_error(settings, api):
    api.add(ep.PERPS_REGISTERED, {"registered": False})
    api.add(ep.PERPS_PUBLIC_PORTFOLIO, (400, {"status": "err", "error": "account not found"}))
    async with session(settings, api) as client:
        data = await call(client, "get_perps_public_account", address=ADDRESS)
    assert data == {"address": ADDRESS, "registered": False}
