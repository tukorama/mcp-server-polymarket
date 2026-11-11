import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio

INDEX = """# Polymarket Documentation

- [API Reference (227 pages)](https://docs.polymarket.com/_llms/en/api-reference.txt): Predictions.

## Predictions

### Trading

- [Fees](https://docs.polymarket.com/trading/fees.md): Understanding trading fees on Polymarket
- [Place Orders](https://docs.polymarket.com/trading/place-orders.md): Tick sizes and order types

## Perps

- [Margin](https://docs.polymarket.com/perps/learn-about-trading/margin.md): Initial margin

## OpenAPI Specs

- [clob-openapi](/api-spec/clob-openapi.yaml)
"""
SUB = """# API Reference Perps

## Perps

- [Get Book](https://docs.polymarket.com/api-reference/get-book.md): Get book for an instrument.
"""
PAGE = """> ## Documentation Index
> Fetch the complete documentation index at: https://docs.polymarket.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Fees

Polymarket charges a small taker fee on certain markets.
"""


async def test_search_ranks_titles_and_filters_products(settings, api):
    api.add(ep.DOCS_INDEX, INDEX)
    api.add(ep.DOCS_SUBINDEX, SUB)
    async with session(settings, api) as client:
        data = await call(client, "search_docs", query="fees")
        assert data["results"][0]["url"] == "https://docs.polymarket.com/trading/fees.md"
        assert data["results"][0]["section"] == "Predictions / Trading"
        assert data["total_pages"] == 4
        perps = await call(client, "search_docs", query="margin book", product="perps")
        assert {r["title"] for r in perps["results"]} == {"Margin", "Get Book"}
        # The index is cached: two searches, one fetch of each index.
        assert len(api.calls(ep.DOCS_INDEX)) == 1


async def test_read_doc_strips_the_banner_and_pages(settings, api):
    api.add(ep.DOCS_PAGE, PAGE, path="/trading/fees.md")
    async with session(settings, api) as client:
        data = await call(client, "read_doc", url="/trading/fees", max_chars=500)
        assert data["text"].startswith("# Fees")
        assert "next_offset" not in data
        first = await call(client, "read_doc", url="https://docs.polymarket.com/trading/fees.md", max_chars=500)
        assert first["url"] == "https://docs.polymarket.com/trading/fees.md"


async def test_read_doc_only_reads_the_docs_site(settings, api):
    async with session(settings, api) as client:
        assert "only reads" in await error(client, "read_doc", url="https://evil.example/x")
        assert "Give a documentation page" in await error(client, "read_doc", url="/../etc")
