"""The confirmation wrapper every money-moving tool goes through, tested on a stand-in tool."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import ElicitResult
from pydantic import Field

from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops.registry import Call, Operation, Runtime
from mcp_server_polymarket.tools import tool_function

pytestmark = pytest.mark.anyio
SENT: list[dict] = []


async def describe(call: Call, amount: str, note: str = "", dry_run: bool = False) -> str:
    return f"Send {amount} pUSD ({note or 'no note'})?"


async def send(
    call: Call,
    amount: Annotated[str, Field(description="How much.")],
    note: str = "",
    dry_run: bool = False,
) -> WriteResult:
    """Pretend to move money."""
    if dry_run:
        return WriteResult(status="preview", dry_run=True)
    SENT.append({"amount": amount, "note": note})
    return WriteResult(status="sent")


def operation(gate: str = "trading") -> Operation:
    return Operation(
        fn=send,
        name="send",
        title="Send",
        toolset="trading",
        requires=(),
        gate=gate,  # type: ignore[arg-type]
        read_only=False,
        idempotent=False,
        destructive=True,
        experimental=False,
        describe=describe,
    )


def server(settings, gate: str = "trading") -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[Runtime]:
        runtime = Runtime.create(settings)
        yield runtime
        await runtime.aclose()

    instance = MCPServer("test", lifespan=lifespan)
    op = operation(gate)
    instance.add_tool(tool_function(op), name=op.name, description=op.description)
    return instance


@pytest.fixture(autouse=True)
def clear():
    SENT.clear()


async def test_confirmation_is_not_an_llm_argument(settings):
    async with Client(server(settings)) as session:
        tool = (await session.list_tools()).tools[0]
    assert set(tool.input_schema["properties"]) == {"amount", "note", "dry_run"}
    assert tool.output_schema


async def test_client_without_elicitation_is_refused(settings):
    async with Client(server(settings)) as session:
        result = await session.call_tool("send", {"amount": "5"})
    assert result.is_error
    assert "cannot ask the user" in result.content[0].text
    assert SENT == []


async def test_dry_run_needs_no_confirmation(settings):
    async with Client(server(settings)) as session:
        result = await session.call_tool("send", {"amount": "5", "dry_run": True})
    assert result.structured_content["status"] == "preview"
    assert SENT == []


async def test_confirm_none_skips_trading_but_not_transfers(settings):
    settings.confirm = "none"
    async with Client(server(settings)) as session:
        result = await session.call_tool("send", {"amount": "5"})
    assert result.structured_content["status"] == "sent"
    async with Client(server(settings, gate="transfers")) as session:
        result = await session.call_tool("send", {"amount": "5"})
    assert result.is_error
    assert len(SENT) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accept_runs_and_shows_the_server_text(settings, mode):
    asked = []

    async def accept(_context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": True})

    async with Client(server(settings), elicitation_callback=accept, mode=mode) as session:
        result = await session.call_tool("send", {"amount": "5", "note": "rent"})
    assert result.structured_content["status"] == "sent"
    assert asked == ["Send 5 pUSD (rent)?"]
    assert SENT == [{"amount": "5", "note": "rent"}]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    "answer",
    [
        ElicitResult(action="decline"),
        ElicitResult(action="cancel"),
        ElicitResult(action="accept", content={"confirm": False}),
    ],
)
async def test_anything_but_yes_sends_nothing(settings, mode, answer):
    async def reply(_context, _params):
        return answer

    async with Client(server(settings), elicitation_callback=reply, mode=mode) as session:
        result = await session.call_tool("send", {"amount": "5"})
    assert result.structured_content["status"] == "declined"
    assert SENT == []
