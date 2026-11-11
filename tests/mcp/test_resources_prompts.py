import json

import pytest

from mcp_server_polymarket import knowledge
from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import session

pytestmark = pytest.mark.anyio


def text_of(result) -> str:
    return result.contents[0].text


async def test_docs_resources(settings, api):
    async with session(settings, api) as client:
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        assert {f"polymarket://docs/{topic}" for topic in knowledge.TOPICS} <= uris
        for topic in knowledge.TOPICS:
            body = text_of(await client.read_resource(f"polymarket://docs/{topic}"))
            assert body.startswith("# ") and len(body) > 1000, topic


async def test_perps_instruments_resource(settings, api):
    api.add(ep.PERPS_INSTRUMENTS, fixture("perps_instruments"))
    async with session(settings, api) as client:
        data = json.loads(text_of(await client.read_resource("polymarket://perps/instruments")))
    assert data["instruments"][0]["instrument_id"] == 1


async def test_market_resource_template(settings, api):
    api.add(ep.GAMMA_MARKET_BY_SLUG, fixture("gamma_market"))
    async with session(settings, api) as client:
        templates = (await client.list_resource_templates()).resource_templates
        assert "polymarket://markets/{slug}" in {t.uri_template for t in templates}
        slug = fixture("gamma_market")["slug"]
        data = json.loads(text_of(await client.read_resource(f"polymarket://markets/{slug}")))
    assert data["slug"] == slug


async def test_prompts(settings, api):
    async with session(settings, api) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert names == {"research_market", "review_portfolio", "plan_perps_trade"}
        prompt = await client.get_prompt("plan_perps_trade", {"symbol": "BTC-USD"})
    text = prompt.messages[0].content.text
    assert "BTC-USD" in text and "Do not place the order" in text


async def test_perps_prompt_follows_the_toolsets(settings, api):
    settings.toolsets = "discovery,market-data,analytics"
    async with session(settings, api) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
    assert "plan_perps_trade" not in names
