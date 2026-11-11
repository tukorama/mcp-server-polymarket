from contextlib import asynccontextmanager
from typing import Any

from mcp import Client

from mcp_server_polymarket.server import create_server


@asynccontextmanager
async def session(settings: Any, api: Any, **client_kwargs: Any):
    async with Client(create_server(settings, transport=api.transport), **client_kwargs) as client:
        yield client


async def call(client: Any, tool: str, /, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content[0].text if result.content else result
    return result.structured_content


async def error(client: Any, tool: str, /, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, result.structured_content
    return result.content[0].text
