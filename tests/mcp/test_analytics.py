import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio
USER = "0x983eedfbd75803602e4a6e6ea9aab6dc6b9c6748"
CONDITION = "0xc017609e846b09a7aca07af50ddaeb53431b77de61f855283e7e41969ae2915c"


async def test_positions_pass_filters_and_drop_noise(settings, api):
    api.add(ep.DATA_POSITIONS, fixture("data_positions"))
    async with session(settings, api) as client:
        data = await call(
            client, "list_positions", user=USER, status="REDEEMABLE", min_value_usd=5, limit=3
        )
    query = api.query(ep.DATA_POSITIONS)
    assert query["status"] == "REDEEMABLE" and query["filter_type"] == "CASH"
    assert query["sort_direction"] == "DESC" and "offset" not in query
    row = data["items"][0]
    assert row["status"] == "REDEEMABLE" and "icon" not in row
    assert row["condition_id"].startswith("0x")
    assert data["has_more"] in (True, False)


async def test_positions_need_an_anchor(settings, api):
    async with session(settings, api) as client:
        assert "Give user" in await error(client, "list_positions")


async def test_cursor_replaces_limit(settings, api):
    api.add(ep.DATA_POSITIONS, fixture("data_positions"))
    async with session(settings, api) as client:
        await call(client, "list_positions", user=USER, cursor="next")
    query = api.query(ep.DATA_POSITIONS)
    assert query["cursor"] == "next" and "limit" not in query


async def test_user_overview_survives_a_failed_section(settings, api):
    api.add(ep.DATA_VALUE, fixture("data_value"))
    api.add(ep.DATA_USER_STATS, fixture("data_user_stats"))
    api.add(ep.DATA_USER_VOLUME, (400, {"error": "bad window"}))
    async with session(settings, api) as client:
        data = await call(client, "get_user_overview", user=USER)
    assert data["value_usd"] == 0.0
    assert data["stats"]["trades"] == 45
    assert data["stats"]["join_date"].endswith("Z")
    assert data["errors"][0]["section"] == "volume"


async def test_user_pnl_downsamples(settings, api):
    points = [{"timestamp": 1_794_000_000 + i * 3600, "economic_pnl": float(i)} for i in range(100)]
    api.add(ep.DATA_USER_PNL, {"data": {"interval": "1m", "fidelity": "1h", "points": points}})
    async with session(settings, api) as client:
        data = await call(client, "get_user_pnl", user=USER, max_points=10)
    assert len(data["points"]) <= 11
    assert data["points"][-1]["economic_pnl"] == 99.0
    assert data["change_economic_pnl"] == 99.0


async def test_feeds(settings, api):
    api.add(ep.DATA_TRADES, fixture("data_trades"))
    api.add(ep.DATA_ACTIVITY, fixture("data_activity"))
    api.add(ep.DATA_COMBO_ACTIVITY, {"data": [], "pagination": {"has_more": False}})
    api.add(ep.DATA_COMBO_POSITIONS, {"data": [], "pagination": {"has_more": False}})
    async with session(settings, api) as client:
        data = await call(client, "list_trades", condition_ids=[CONDITION, "0xb"], min_usd=100)
        assert api.query(ep.DATA_TRADES)["condition"] == f"{CONDITION},0xb"
        assert data["items"][0]["timestamp"].endswith("Z")
        data = await call(client, "list_activity", user=USER, types=["DEPOSIT", "TRADE"])
        query = api.query(ep.DATA_ACTIVITY)
        assert query["type"] == "DEPOSIT,TRADE"
        assert query["exclude_deposits_withdrawals"] == "false"
        rebate = data["items"][0]
        assert "outcome_index" not in rebate and "token_id" not in rebate
        await call(client, "list_combo_activity", user=USER)
        await call(client, "list_combo_positions", user=USER, status="OPEN")


async def test_market_state(settings, api):
    api.add(ep.DATA_HOLDERS, fixture("data_holders"))
    api.add(ep.DATA_OPEN_INTEREST, fixture("data_oi"))
    api.add(ep.DATA_LIVE_VOLUME, fixture("data_live_volume"))
    api.add(ep.DATA_RESOLUTIONS, fixture("data_resolutions"))
    api.add(ep.DATA_TOKENS, fixture("data_tokens"))
    async with session(settings, api) as client:
        data = await call(client, "list_market_holders", condition_id=CONDITION, include_pnl=True)
        assert data["tokens"][0]["holders"][0]["amount"] > 0
        assert api.query(ep.DATA_HOLDERS)["include_pnl"] == "true"
        data = await call(client, "get_open_interest", condition_ids=[CONDITION])
        assert data["total"] == pytest.approx(650111.798427)
        data = await call(client, "get_event_live_volume", event_id="1")
        assert data["taker_volume_total"] > 0
        data = await call(client, "get_resolutions", condition_ids=[CONDITION])
        assert data["items"][0]["status"] == "posed"
        assert "exactly one" in await error(client, "get_resolutions")
        data = await call(client, "lookup_tokens", condition_ids=[CONDITION])
        assert data["items"][0]["module"] == "v1_ctf"


async def test_boards_and_approvals(settings, api):
    api.add(ep.DATA_LEADERBOARD, fixture("data_leaderboard"))
    api.add(ep.DATA_BIGGEST_WINNERS, fixture("data_biggest_winners"))
    api.add(ep.DATA_APPROVALS, fixture("data_approvals"))
    async with session(settings, api) as client:
        data = await call(client, "get_leaderboard", window="week", sort_by="VOLUME")
        assert data["items"][0]["rank"] == 1 and data["next_cursor"]
        assert api.query(ep.DATA_LEADERBOARD)["time_period"] == "week"
        data = await call(client, "list_biggest_wins")
        assert data["items"][0]["resolved_at"].endswith("Z")
        data = await call(client, "get_wallet_approvals", user=USER)
        assert data["contracts"][0]["approved"] is True


async def test_leaderboard_standing_of_one_user(settings, api):
    api.add(ep.DATA_LEADERBOARD, {"data": {"user_id": USER, "pnl_rank": 9, "volume_rank": 3}})
    async with session(settings, api) as client:
        data = await call(client, "get_leaderboard", user=USER)
    assert data["items"] == [{"user_id": USER, "pnl_rank": 9, "volume_rank": 3}]
    assert "limit" not in api.query(ep.DATA_LEADERBOARD)
