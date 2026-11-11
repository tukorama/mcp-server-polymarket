"""Run a Polymarket research agent.

    uv run --extra langgraph --extra signing --with langchain-anthropic \
        python examples/langgraph_agent.py
    uv run --extra langgraph --with "langchain[mcp]" --with langchain-anthropic \
        python examples/langgraph_agent.py --mcp

Set ANTHROPIC_API_KEY. Reading needs no Polymarket account. With POLYMARKET_ALLOW_TRADING=1 and
a configured wallet the native agent can also place orders: each one pauses here for a yes/no.
"""

import argparse
import asyncio
from pathlib import Path

MODEL = "anthropic:claude-sonnet-5-5"
QUESTION = (
    "What are the three most traded markets about the next Fed decision, and how do their "
    "order books look? Also give me the current BTC perp funding rate."
)
PROJECT = str(Path(__file__).resolve().parent.parent)


async def native() -> None:
    from langgraph.types import Command

    from mcp_server_polymarket.integrations.langgraph import create_polymarket_agent

    agent = create_polymarket_agent(MODEL)
    config = {"configurable": {"thread_id": "example"}}
    result = await agent.ainvoke({"messages": [("user", QUESTION)]}, config=config)
    while result.get("__interrupt__"):
        pause = result["__interrupt__"][0].value
        answer = input(f"\n{pause['message']}\n[y/N] ").strip().lower() == "y"
        result = await agent.ainvoke(Command(resume=answer), config=config)
    print(result["messages"][-1].content)


async def over_mcp() -> None:
    from langchain.agents import create_agent
    from langchain.mcp import MCPAdapter

    config = {
        "mcpServers": {
            "polymarket": {
                "command": "uvx",
                "args": ["--from", PROJECT, "mcp-server-polymarket"],
                # The subprocess only inherits HOME, PATH and a few others. Pass POLYMARKET_*
                # here if you need them. Timeouts are in milliseconds.
                "timeout": 120_000,
            }
        }
    }
    async with MCPAdapter(config) as adapter:
        agent = create_agent(MODEL, await adapter.list_tools())
        result = await agent.ainvoke({"messages": [("user", QUESTION)]})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", action="store_true", help="use the MCP server instead")
    asyncio.run(over_mcp() if parser.parse_args().mcp else native())
