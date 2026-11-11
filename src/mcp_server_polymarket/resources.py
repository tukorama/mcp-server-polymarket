from collections.abc import Callable

from mcp.server import MCPServer

from mcp_server_polymarket import knowledge
from mcp_server_polymarket.ops.registry import Call, Runtime


def _add_doc(server: MCPServer, topic: str) -> None:
    def read_doc() -> str:
        return knowledge.read(topic)

    read_doc.__name__ = topic.replace("-", "_")
    server.resource(
        f"polymarket://docs/{topic}",
        name=topic,
        description=f"How this server and Polymarket work: {topic.replace('-', ' ')}.",
        mime_type="text/markdown",
    )(read_doc)


def register_resources(server: MCPServer, runtime: Callable[[], Runtime]) -> None:
    # The SDK does not inject Context into static resources, hence the `runtime` getter.
    @server.resource(
        "polymarket://perps/instruments",
        description="Every listed Perps instrument with its id, symbol and trading limits.",
        mime_type="application/json",
    )
    async def perps_instruments() -> str:
        from mcp_server_polymarket.ops import perps_market

        result = await perps_market.list_perps_instruments(Call(runtime=runtime()))
        return result.model_dump_json()

    @server.resource(
        "polymarket://markets/{slug}",
        description="One prediction market by its URL slug.",
        mime_type="application/json",
    )
    async def market(slug: str) -> str:
        from mcp_server_polymarket.ops import discovery

        result = await discovery.get_market(Call(runtime=runtime()), slug=slug)
        return result.model_dump_json()

    for topic in knowledge.TOPICS:
        _add_doc(server, topic)
