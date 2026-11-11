import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio

MARKET = fixture("gamma_market")
EVENT = fixture("gamma_event")


async def test_search_trims_events_and_pages(settings, api):
    api.add(ep.GAMMA_SEARCH, fixture("gamma_search"))
    async with session(settings, api) as client:
        data = await call(client, "search", query="bitcoin", limit=2, max_markets=1)
    assert data["events"][0]["title"]
    assert len(data["events"][0]["markets"]) <= 1
    assert api.query(ep.GAMMA_SEARCH) == {
        "q": "bitcoin",
        "events_status": "active",
        "limit_per_type": "2",
        "page": "1",
        "keep_closed_markets": "0",
    }


async def test_list_events_uses_keyset_paging(settings, api):
    api.add(ep.GAMMA_EVENTS_KEYSET, fixture("gamma_events_keyset"))
    async with session(settings, api) as client:
        data = await call(client, "list_events", tag_slug="politics", cursor="abc", limit=2)
    query = api.query(ep.GAMMA_EVENTS_KEYSET)
    assert query["after_cursor"] == "abc"
    assert query["order"] == "volume24hr" and query["ascending"] == "false"
    assert query["closed"] == "false" and query["tag_slug"] == "politics"
    assert data["next_cursor"]
    event = data["items"][0]
    assert event["url"].startswith("https://polymarket.com/event/")
    assert event["markets_total"] >= len(event["markets"])


async def test_list_events_with_archived_uses_offset_paging(settings, api):
    api.add(ep.GAMMA_EVENTS, [EVENT, EVENT])
    async with session(settings, api) as client:
        data = await call(client, "list_events", include_archived=True, limit=2, cursor="o:4")
    assert api.query(ep.GAMMA_EVENTS)["offset"] == "4"
    assert data["next_cursor"] == "o:6"


async def test_bad_cursor_is_explained(settings, api):
    async with session(settings, api) as client:
        text = await error(client, "list_tags", cursor="garbage")
    assert "Pass next_cursor exactly" in text


async def test_get_event_by_slug_with_counts(settings, api):
    api.add(ep.GAMMA_EVENT_BY_SLUG, EVENT)
    api.add(ep.GAMMA_EVENT_COMMENT_COUNT, {"count": 7})
    api.add(ep.GAMMA_EVENT_TWEETS, {"tweetCount": 2})
    async with session(settings, api) as client:
        data = await call(client, "get_event", slug=EVENT["slug"], include_counts=True)
    assert data["id"] == EVENT["id"]
    assert data["comment_count"] == 7 and data["tweet_count"] == 2
    volumes = [m.get("volume") or 0 for m in data["markets"]]
    assert volumes == sorted(volumes, reverse=True)


async def test_get_event_fetches_tags_when_missing(settings, api):
    api.add(ep.GAMMA_EVENT, {**EVENT, "tags": []})
    api.add(ep.GAMMA_EVENT_TAGS, fixture("gamma_event_tags"))
    async with session(settings, api) as client:
        data = await call(client, "get_event", event_id=EVENT["id"])
    assert data["tags"][0]["slug"]


async def test_get_event_needs_one_identifier(settings, api):
    async with session(settings, api) as client:
        assert "exactly one" in await error(client, "get_event")


async def test_get_market_parses_outcomes_and_keeps_full_rules(settings, api):
    api.add(ep.GAMMA_MARKET_BY_SLUG, {**MARKET, "tags": [{"id": "2", "label": "Politics"}]})
    async with session(settings, api) as client:
        data = await call(client, "get_market", slug=MARKET["slug"])
    names = [o["name"] for o in data["outcomes"]]
    assert names == ["Yes", "No"]
    assert all(o["token_id"] and o["price"] is not None for o in data["outcomes"])
    assert len(data["description"]) > 300
    assert data["tick_size"] == MARKET["orderPriceMinTickSize"]


async def test_get_market_by_condition_falls_back_for_tags_and_description(settings, api):
    bare = {**MARKET, "description": "", "tags": None}
    api.add(ep.GAMMA_MARKETS_KEYSET, {"markets": [bare], "next_cursor": None})
    api.add(ep.GAMMA_MARKET_TAGS, fixture("gamma_market_tags"))
    api.add(ep.GAMMA_MARKET_DESCRIPTION, fixture("gamma_market_desc"))
    async with session(settings, api) as client:
        data = await call(client, "get_market", condition_id=MARKET["conditionId"])
    assert api.query(ep.GAMMA_MARKETS_KEYSET)["condition_ids"] == MARKET["conditionId"]
    assert data["description"].startswith("The next French")
    assert data["tags"]


async def test_get_market_by_id_and_token(settings, api):
    api.add(ep.GAMMA_MARKET, MARKET)
    api.add(ep.GAMMA_MARKETS_KEYSET, {"markets": [MARKET]})
    async with session(settings, api) as client:
        assert (await call(client, "get_market", market_id=MARKET["id"]))["id"] == MARKET["id"]
        token = "123"
        await call(client, "get_market", token_id=token)
    assert api.query(ep.GAMMA_MARKETS_KEYSET)["clob_token_ids"] == token


async def test_v2_markets_trade_position_ids(settings, api):
    v2 = {**MARKET, "version": "v2", "positionIds": '["11", "22"]'}
    api.add(ep.GAMMA_MARKET, v2)
    async with session(settings, api) as client:
        data = await call(client, "get_market", market_id="1")
    assert [o["token_id"] for o in data["outcomes"]] == ["11", "22"]
    assert data["protocol"] == "v2"


async def test_list_markets_routes_by_query(settings, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, fixture("gamma_markets_keyset"))
    api.add(ep.GAMMA_MARKETS, [MARKET])
    api.add(ep.GAMMA_MARKETS_ABRIDGED, fixture("gamma_markets_abridged"))
    api.add(ep.GAMMA_MARKETS_INFORMATION, fixture("gamma_markets_information"))
    async with session(settings, api) as client:
        page = await call(client, "list_markets", order="liquidity", limit=2)
        assert api.query(ep.GAMMA_MARKETS_KEYSET)["order"] == "liquidityNum"
        assert page["items"][0]["outcomes"]
        await call(client, "list_markets", min_rewards_size=100, limit=1)
        assert api.query(ep.GAMMA_MARKETS)["rewards_min_size"] == "100.0"
        await call(client, "list_markets", condition_ids=["0xa"], abridged=True)
        assert api.body(ep.GAMMA_MARKETS_ABRIDGED) == {"conditionIds": ["0xa"], "closed": False}
        many = [f"0x{i}" for i in range(25)]
        await call(client, "list_markets", condition_ids=many)
        assert len(api.body(ep.GAMMA_MARKETS_INFORMATION)["conditionIds"]) == 25


async def test_tags(settings, api):
    api.add(ep.GAMMA_TAGS, fixture("gamma_tags"))
    api.add(ep.GAMMA_TAG_BY_SLUG, {"id": "2", "label": "Politics", "slug": "politics"})
    api.add(ep.GAMMA_TAG, {"id": "2", "label": "Politics", "slug": "politics"})
    api.add(ep.GAMMA_TAG_RELATED_BY_SLUG, [{"id": "126", "label": "Trump", "slug": "trump"}])
    api.add(ep.GAMMA_TAG_RELATED, [{"id": "126", "label": "Trump", "slug": "trump"}])
    api.add(ep.GAMMA_TAG_RELATIONS, [{"id": "1", "tagID": 2, "relatedTagID": 126, "rank": 1}])
    api.add(ep.GAMMA_TAG_RELATIONS_BY_SLUG, [{"tagID": 2, "relatedTagID": 126, "rank": 1}])
    async with session(settings, api) as client:
        page = await call(client, "list_tags", limit=3)
        assert page["next_cursor"] == "o:3"
        data = await call(client, "get_tag", slug="politics")
        assert data["related_tags"][0]["slug"] == "trump"
        await call(client, "get_tag", tag_id="2")
        data = await call(client, "get_tag", tag_id="2", related="relations")
        assert data["relations"] == [{"tag_id": "2", "related_tag_id": "126", "rank": 1}]
        await call(client, "get_tag", slug="politics", related="relations")


async def test_series(settings, api):
    api.add(ep.GAMMA_SERIES_LIST, fixture("gamma_series"))
    api.add(ep.GAMMA_SERIES_SUMMARY_BY_SLUG, fixture("gamma_series_summary"))
    api.add(ep.GAMMA_SERIES_SUMMARY, fixture("gamma_series_summary"))
    api.add(ep.GAMMA_SERIES, fixture("gamma_series_one"))
    api.add(ep.GAMMA_SERIES_COMMENT_COUNT, {"count": 3})
    async with session(settings, api) as client:
        assert (await call(client, "list_series", limit=1))["items"][0]["slug"] == "nfl"
        data = await call(client, "get_series", slug="nfl", max_events=1)
        assert data["comment_count"] == 3 and len(data.get("events", [])) <= 1
        data = await call(client, "get_series", series_id="1", summary_only=True)
        assert data["event_dates"]


async def test_comments_are_cleaned(settings, api):
    rows = fixture("gamma_comments")
    rows[0]["body"] = "hi‮ there\x07"
    api.add(ep.GAMMA_COMMENTS, rows)
    api.add(ep.GAMMA_COMMENT, rows[:1])
    api.add(ep.GAMMA_COMMENTS_BY_USER, rows)
    async with session(settings, api) as client:
        data = await call(client, "list_comments", event_id=1, limit=2)
        assert data["items"][0]["body"] == "hi there"
        assert api.query(ep.GAMMA_COMMENTS)["parent_entity_type"] == "Event"
        await call(client, "list_comments", comment_id="5")
        await call(client, "list_comments", user_address="0x" + "1" * 40)
        assert "Give one of" in await error(client, "list_comments")


async def test_profile_falls_back_to_the_older_route(settings, api):
    api.add(ep.GAMMA_PUBLIC_PROFILE, (404, {"error": "not found"}))
    api.add(ep.GAMMA_PROFILE_BY_USER, fixture("gamma_profile"))
    async with session(settings, api) as client:
        data = await call(
            client, "get_profile", address="0x983eedfbd75803602e4a6e6ea9aab6dc6b9c6748"
        )
    assert data["taker_tier"] == "Platinum"


async def test_event_creators(settings, api):
    api.add(ep.GAMMA_EVENT_CREATORS, fixture("gamma_creators"))
    api.add(ep.GAMMA_EVENT_CREATOR, fixture("gamma_creators")[0])
    async with session(settings, api) as client:
        assert (await call(client, "list_event_creators", limit=2))["items"][1]["handle"]
        assert (await call(client, "list_event_creators", creator_id="1"))["items"][0]["id"] == "1"
