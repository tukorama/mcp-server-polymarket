"""Read tools in the opt-in toolsets: sports, rewards, bridge, combos, builder, Perps extras."""

import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio
WALLET = "0x983eEdfBD75803602E4A6E6Ea9aab6dC6B9C6748"


@pytest.fixture
def everything(settings):
    settings.toolsets = "all"
    settings.wallet_address = WALLET
    settings.signature_type = 3
    settings.api_key, settings.api_secret, settings.api_passphrase = "k", "c2VjcmV0", "p"
    settings.address = "0x" + "1" * 40
    settings.relayer_api_key, settings.relayer_api_key_address = "rk", "0x" + "3" * 40
    settings.perps_proxy, settings.perps_secret = "0x" + "2" * 40, "ps"
    return settings


async def test_sports(everything, api):
    api.add(ep.GAMMA_SPORTS, fixture("gamma_sports"))
    api.add(ep.GAMMA_SPORTS_MARKET_TYPES, fixture("gamma_market_types"))
    api.add(ep.GAMMA_TEAMS, fixture("gamma_teams"))
    api.add(ep.GAMMA_TEAM, fixture("gamma_teams")[0])
    api.add(ep.GAMMA_EVENTS_RESULTS, fixture("gamma_results"))
    async with session(everything, api) as client:
        data = await call(client, "get_sports_metadata", limit=2)
        assert len(data["sports"]) <= 2 and data["total"] >= 1
        assert "image" not in data["sports"][0]
        await call(client, "get_sports_metadata", search="zzz")
        assert len(api.calls(ep.GAMMA_SPORTS)) == 1, "cached"
        data = await call(client, "get_sports_metadata", kind="market_types")
        assert data["market_types"]
        data = await call(client, "list_teams", league="nba", limit=2)
        assert api.query(ep.GAMMA_TEAMS)["league"] == "nba" and data["next_cursor"] == "o:2"
        assert (await call(client, "list_teams", team_id="1"))["items"]
        assert (await call(client, "list_sports_results"))["items"]


async def test_rewards(everything, api):
    api.add(ep.CLOB_REWARDS_MULTI, fixture("clob_rewards_multi"))
    api.add(ep.CLOB_REWARDS_CURRENT, fixture("clob_rewards_current"))
    api.add(ep.CLOB_REWARDS_MARKET, fixture("clob_rewards_market"))
    api.add(ep.CLOB_REBATES, {"data": [{"condition_id": "0xa", "rebated_fees_usdc": 1.5}]})
    api.add(ep.CLOB_USER_EARNINGS_TOTAL, [{"asset_address": "0xusdc", "earnings": 2.1}])
    api.add(
        ep.CLOB_USER_EARNINGS,
        {"data": [{"condition_id": "0xa", "earnings": 1}], "next_cursor": "LTE="},
    )
    api.add(ep.CLOB_USER_REWARD_PERCENTAGES, {"0xa": 12.5})
    api.add(ep.CLOB_USER_REWARD_MARKETS, {"data": [], "next_cursor": "MTAw"})
    api.add(ep.CLOB_ORDER_SCORING, {"scoring": True})
    api.add(ep.CLOB_ORDERS_SCORING, {"0x1": True, "0x2": False})
    async with session(everything, api) as client:
        data = await call(client, "list_reward_markets", query="trump", max_spread=4)
        assert data["items"][0]["rewards_min_size"] and data["next_cursor"]
        assert api.query(ep.CLOB_REWARDS_MULTI)["order_by"] == "rate_per_day"
        await call(client, "list_reward_markets", all_configs=True)
        data = await call(client, "list_reward_markets", condition_id="0xa")
        assert "next_cursor" not in data
        data = await call(client, "get_maker_rebates", date="2026-11-10")
        assert api.query(ep.CLOB_REBATES) == {"date": "2026-11-10", "maker_address": WALLET}
        data = await call(client, "get_my_reward_earnings", date="2026-11-10")
        assert api.query(ep.CLOB_USER_EARNINGS_TOTAL)["signature_type"] == "3"
        assert data["total"][0]["earnings"] == 2.1
        data = await call(client, "get_my_reward_earnings", view="by_market")
        assert data["by_market"][0]["condition_id"] == "0xa"
        data = await call(client, "get_my_reward_earnings", view="live_percentages")
        assert data["percentages"] == {"0xa": 12.5}
        data = await call(client, "list_my_reward_markets", only_open_orders=True)
        assert data["next_cursor"] == "MTAw"
        data = await call(client, "get_my_order_scoring", order_ids=["0x9"])
        assert data["scoring"] == {"0x9": True}
        data = await call(client, "get_my_order_scoring", order_ids=["0x1", "0x2"])
        assert api.body(ep.CLOB_ORDERS_SCORING) == ["0x1", "0x2"]
        assert "not a date" in await error(client, "get_maker_rebates", date="soon")
    l2 = api.calls(ep.CLOB_USER_EARNINGS)[0].headers
    assert l2["POLY_API_KEY"] == "k" and l2["POLY_SIGNATURE"]


async def test_bridge(everything, api):
    api.add(ep.BRIDGE_ASSETS, fixture("bridge_assets"))
    api.add(ep.BRIDGE_QUOTE, {"quoteId": "q1", "estOutputUsd": 9.9})
    api.add(ep.BRIDGE_STATUS, {"transactions": [{"status": "COMPLETED"}], "nextCursor": "c2"})
    api.add(ep.BRIDGE_DEPOSIT, {"address": {"evm": "0xdep", "svm": "So1"}, "note": "send here"})
    everything.builder_code = "0x" + "ab" * 32
    async with session(everything, api) as client:
        data = await call(client, "list_bridge_assets", chain="ethereum")
        assert data["assets"] and all(a["chain"] == "Ethereum" for a in data["assets"])
        quote = await call(
            client,
            "get_bridge_quote",
            amount_base_units="1000000",
            from_chain_id="1",
            from_token_address="0x" + "a" * 40,
            to_chain_id="137",
            to_token_address="0x" + "b" * 40,
        )
        assert quote["quoteId"] == "q1"
        assert api.body(ep.BRIDGE_QUOTE)["recipientAddress"] == WALLET
        status = await call(client, "get_bridge_status", address="0xdep")
        assert status["next_cursor"] == "c2"
        data = await call(client, "create_deposit_addresses")
        assert data["addresses"]["evm"] == "0xdep"
    assert api.body(ep.BRIDGE_DEPOSIT) == {"address": WALLET}
    assert api.calls(ep.BRIDGE_DEPOSIT)[0].headers["X-Builder-Code"] == everything.builder_code


async def test_combo_reads(everything, api):
    api.add(ep.COMBO_MARKETS, fixture("combo_markets"))
    api.add(ep.COMBO_REQUEST, {"id": "r1", "state": "FILLED"})
    api.add(
        ep.COLLATERAL_RETURN_PLAN,
        {"plan_hash": "0xph", "operations": [{"kind": "merge"}], "router_call": {"data": "0x"}},
    )
    async with session(everything, api) as client:
        data = await call(client, "list_combo_markets", limit=2)
        outcome = data["items"][0]["outcomes"][0]
        assert outcome["name"] == "Yes" and outcome["position_id"]
        assert (await call(client, "get_combo_request", request_id="r1"))["state"] == "FILLED"
        plan = await call(client, "plan_collateral_return")
        assert plan["plan_hash"] == "0xph" and "router_call" not in plan
    assert api.body(ep.COLLATERAL_RETURN_PLAN) == {"wallet": WALLET}


async def test_builder_reads_hide_secrets(everything, api):
    api.add(ep.DATA_BUILDER_LEADERBOARD, fixture("data_builders"))
    api.add(ep.DATA_BUILDER_VOLUME, fixture("data_builder_volume"))
    api.add(ep.CLOB_BUILDER_TRADES, {"data": [{"id": "t1"}], "next_cursor": "LTE="})
    api.add(ep.CLOB_BUILDER_KEYS, [{"key": "bk", "secret": "s", "passphrase": "p"}])
    api.add(ep.RELAYER_API_KEYS, [{"apiKey": "rk2", "secret": "s"}])
    api.add(ep.PERPS_BUILDER_EARNINGS_SUMMARY, {"settled": "1.0"})
    api.add(ep.PERPS_BUILDER_EARNINGS, {"data": [{"fee": "0.1"}], "cursor": "n"})
    api.add(ep.PERPS_BUILDER_APPROVALS, [{"builder": "0xb", "max_fee_rate": "0.001"}])
    async with session(everything, api) as client:
        assert (await call(client, "get_builder_leaderboard"))["items"][0]["builder_name"]
        await call(client, "get_builder_leaderboard", view="volume_series", window="week")
        assert api.query(ep.DATA_BUILDER_VOLUME)["interval"] == "week"
        assert "builder_code" in await error(client, "list_builder_trades")
        data = await call(client, "list_builder_trades", builder_code="0xcode")
        assert data["items"] == [{"id": "t1"}]
        keys = await call(client, "list_builder_api_keys")
        assert keys["keys"] == [{"key": "bk"}]
        relayer = await call(client, "list_relayer_api_keys")
        assert relayer["keys"] == [{"apiKey": "rk2"}]
        assert (await call(client, "get_my_perps_builder"))["summary"] == {"settled": "1.0"}
        receipts = await call(client, "get_my_perps_builder", view="receipts", cursor="c")
        assert receipts["next_cursor"] == "n"
        approvals = await call(client, "get_my_perps_builder", view="approvals", builder="0xb")
        assert approvals["approvals"][0]["max_fee_rate"] == "0.001"
    headers = api.calls(ep.RELAYER_API_KEYS)[0].headers
    assert headers["RELAYER_API_KEY"] == "rk"
    assert api.calls(ep.PERPS_BUILDER_EARNINGS)[0].headers["POLYMARKET-SECRET"] == "ps"


async def test_perps_extras(everything, api):
    api.add(ep.PERPS_LEADERBOARD, fixture("perps_leaderboard"))
    api.add(ep.PERPS_COMPETITIONS, fixture("perps_competitions"))
    api.add(ep.PERPS_COMPETITION_WIDGET, fixture("perps_widget"))
    api.add(ep.PERPS_COMPETITION, fixture("perps_competition"))
    api.add(ep.PERPS_COMPETITION_LEADERBOARD, fixture("perps_competition_lb"))
    api.add(ep.PERPS_COMPETITION_ACCOUNT, fixture("perps_comp_account"))
    api.add(ep.PERPS_POSITION_SNAPSHOTS, {"cards": []})
    api.add(ep.PERPS_BLP_ENROLLMENT, {"enrolled": []})
    api.add(ep.PERPS_BLP_LIQUIDATIONS, {"data": [{"instrument_id": 1}], "cursor": None})
    address = "0x" + "5" * 40
    async with session(everything, api) as client:
        board = await call(client, "get_perps_leaderboard", sort_by="roi")
        assert board["entries"] and api.query(ep.PERPS_LEADERBOARD)["sort_by"] == "roi"
        data = await call(client, "list_perps_competitions", address=address)
        assert data["competitions"] and data["live_summary"]["competition_id"] == 1
        data = await call(client, "get_perps_competition", competition_id=1, address=address)
        assert data["competition"] and data["leaderboard"] and data["account"]
        await call(client, "get_perps_position_snapshots", address=address, instrument_ids=[1])
        assert api.body(ep.PERPS_POSITION_SNAPSHOTS)["active_instrument_ids"] == [1]
        assert "Give instrument_ids" in await error(
            client, "get_perps_position_snapshots", address=address
        )
        blp = await call(client, "get_my_perps_blp")
        assert blp["liquidations"] == [{"instrument_id": 1}]
