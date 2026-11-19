from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")
pytest.importorskip("eth_account")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from mcp import Client

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.session import ClobCredentials, CredentialStore
from mcp_server_polymarket.integrations import langgraph as integration
from mcp_server_polymarket.ops import Runtime
from mcp_server_polymarket.server import create_server
from mcp_server_polymarket.signing.wallets import derive_deposit_wallet
from tests.conftest import fixture

pytestmark = pytest.mark.anyio

README = Path(__file__).parents[2] / "README.md"
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TOKEN = "60133385087854467667298832945990887425905703052255634714562642069595733878370"
BOOK = {
    **fixture("clob_book"),
    "asset_id": TOKEN,
    "bids": [{"price": "0.40", "size": "100"}],
    "asks": [{"price": "0.42", "size": "80"}],
    "tick_size": "0.01",
    "min_order_size": "5",
    "neg_risk": False,
}
ORDER = {"token_id": TOKEN, "side": "BUY", "price": "0.41", "size": "10"}


def inject(monkeypatch: Any, api: Any) -> Any:
    original = Runtime.create

    def fake_create(cls: Any, settings: Any, **kw: Any) -> Any:
        return original(settings, **{**kw, "transport": api.transport})

    monkeypatch.setattr(integration.Runtime, "create", classmethod(fake_create))


@pytest.fixture
def trader(settings: Any, api: Any, monkeypatch: Any) -> Any:
    settings.private_key = KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    settings.allow_trading = True
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    store.save_clob(ClobCredentials(OWNER, "owner-key", "c2VjcmV0", "pass"))
    api.add(ep.CLOB_BOOK, BOOK)
    api.add(ep.GAMMA_MARKETS_KEYSET, {"markets": []})
    api.add(ep.WEB_GEOBLOCK, {"blocked": False})
    api.add(ep.CLOB_POST_ORDER, {"success": True, "orderID": "0xabc", "status": "live"})
    inject(monkeypatch, api)
    return settings


async def test_langchain_schemas_match_the_mcp_tools(trader: Any, api: Any) -> None:
    async with Client(create_server(trader, transport=api.transport)) as session:
        mcp_tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    tools = integration.get_polymarket_tools(trader, include_gated=True)
    assert {tool.name for tool in tools} == set(mcp_tools)
    for tool in tools:
        assert tool.args_schema is not None
        schema: dict[str, Any] = (
            tool.args_schema
            if isinstance(tool.args_schema, dict)
            else cast("Any", tool.args_schema).model_json_schema()
        )
        assert set(schema.get("properties", {})) == set(
            mcp_tools[tool.name].input_schema.get("properties", {})
        ), tool.name
        assert tool.description == mcp_tools[tool.name].description


async def test_gated_tools_are_excluded_unless_requested(trader: Any) -> None:
    names = {t.name for t in integration.get_polymarket_tools(trader)}
    assert "place_limit_order" not in names
    assert "list_my_open_orders" in names


async def test_toolsets_and_prefix(settings: Any) -> None:
    tools = integration.get_polymarket_tools(settings, toolsets="perps-market", prefix="pm_")
    assert tools
    assert all(t.name.startswith("pm_") and "perps" in t.name for t in tools)


async def test_async_tool_returns_plain_json(settings: Any, api: Any, monkeypatch: Any) -> None:
    inject(monkeypatch, api)
    api.add(ep.PERPS_INSTRUMENTS, fixture("perps_instruments"))
    tool = next(
        t for t in integration.get_polymarket_tools(settings) if t.name == "list_perps_instruments"
    )
    result = await tool.ainvoke({})
    assert isinstance(result, dict)
    assert result["instruments"]


def test_sync_tool_works_too(settings: Any, api: Any, monkeypatch: Any) -> None:
    inject(monkeypatch, api)
    api.add(ep.PERPS_INSTRUMENTS, fixture("perps_instruments"))
    tool = next(
        t for t in integration.get_polymarket_tools(settings) if t.name == "list_perps_instruments"
    )
    assert tool.invoke({})["instruments"]


async def test_errors_reach_the_model_as_text(settings: Any, api: Any, monkeypatch: Any) -> None:
    inject(monkeypatch, api)
    api.add(ep.GAMMA_MARKET_BY_SLUG, (404, {"error": "not found"}))
    tool = next(t for t in integration.get_polymarket_tools(settings) if t.name == "get_market")
    assert isinstance(await tool.ainvoke({"slug": "missing"}), str)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (True, True),
        ("yes", True),
        ({"type": "approve"}, True),
        ({"confirm": True}, True),
        (False, False),
        ("no", False),
        ({"type": "reject"}, False),
        (None, False),
        (1, False),
    ],
)
def test_approved(answer: Any, expected: Any) -> None:
    assert integration.approved(answer) is expected


def tool_call_model(name: str, args: dict[str, Any]) -> Any:
    class BindableFake(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    messages = iter(
        [
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}]),
            AIMessage(content="done"),
        ]
    )
    return BindableFake(messages=messages)


async def test_agent_pauses_with_the_confirmation_text(trader: Any, api: Any) -> None:
    agent = integration.create_polymarket_agent(
        tool_call_model("place_limit_order", ORDER), settings=trader
    )
    config = {"configurable": {"thread_id": "t1"}}
    state = await agent.ainvoke({"messages": [("user", "buy")]}, config=config)
    pause = state["__interrupt__"][0].value
    assert pause["tool"] == "place_limit_order"
    assert "pay up to $4.10" in pause["message"]
    assert api.calls(ep.CLOB_POST_ORDER) == []
    state = await agent.ainvoke(Command(resume=True), config=config)
    assert len(api.calls(ep.CLOB_POST_ORDER)) == 1
    assert state["messages"][-1].content == "done"


async def test_agent_declines_on_anything_but_yes(trader: Any, api: Any) -> None:
    agent = integration.create_polymarket_agent(
        tool_call_model("place_limit_order", ORDER), settings=trader
    )
    config = {"configurable": {"thread_id": "t2"}}
    await agent.ainvoke({"messages": [("user", "buy")]}, config=config)
    state = await agent.ainvoke(Command(resume="maybe later"), config=config)
    assert api.calls(ep.CLOB_POST_ORDER) == []
    assert "did not confirm" in str(state["messages"][-2].content)


async def test_dry_run_does_not_pause(trader: Any, api: Any) -> None:
    agent = integration.create_polymarket_agent(
        tool_call_model("place_limit_order", {**ORDER, "dry_run": True}), settings=trader
    )
    state = await agent.ainvoke(
        {"messages": [("user", "preview")]}, config={"configurable": {"thread_id": "t3"}}
    )
    assert "__interrupt__" not in state
    assert api.calls(ep.CLOB_POST_ORDER) == []


async def test_read_only_agent_needs_no_checkpointer(
    settings: Any, api: Any, monkeypatch: Any
) -> None:
    inject(monkeypatch, api)
    api.add(ep.PERPS_INSTRUMENTS, fixture("perps_instruments"))
    agent = integration.create_polymarket_agent(
        tool_call_model("list_perps_instruments", {}), settings=settings
    )
    state = await agent.ainvoke({"messages": [("user", "show")]})
    assert state["messages"][-1].content == "done"


def test_readme_graph_is_current(settings: Any) -> None:
    agent = integration.create_polymarket_agent(tool_call_model("search", {}), settings=settings)
    drawn = integration.draw_mermaid(agent)
    assert drawn in README.read_text(encoding="utf-8"), f"Paste this graph into README.md:\n{drawn}"


def test_graph_marks_gated_tools(trader: Any) -> None:
    agent = integration.create_polymarket_agent(tool_call_model("search", {}), settings=trader)
    drawn = integration.draw_mermaid(agent)
    assert ":::funds" in drawn
    assert "place_limit_order" in drawn


def test_graph_draws_a_hand_built_graph_with_foreign_tools(settings: Any) -> None:
    def lookup_ticker(name: str) -> str:
        return name

    def keep_state(state: Any) -> Any:
        return state

    def stop_branch(_state: Any) -> str:
        return "stop"

    foreign = StructuredTool.from_function(
        lookup_ticker, name="lookup_ticker", description="Look up a ticker."
    )
    builder: Any = StateGraph(MessagesState)
    builder.add_node("end", keep_state)
    builder.add_node("call", ToolNode([*integration.get_polymarket_tools(settings), foreign]))
    builder.add_edge(START, "end")
    builder.add_conditional_edges("end", stop_branch, {"more": "call", "stop": END})
    builder.add_edge("call", "end")
    drawn = integration.draw_mermaid(builder.compile())
    assert 'n1["end"]' in drawn
    assert 'n1 -.->|"more"| n2' in drawn
    assert 'tools_other_all["lookup_ticker"]' in drawn
    assert "place_limit_order" not in drawn
