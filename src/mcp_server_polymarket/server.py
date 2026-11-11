from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp.server import MCPServer

from mcp_server_polymarket import __version__, knowledge
from mcp_server_polymarket.capabilities import Capabilities
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.logging_setup import configure_logging
from mcp_server_polymarket.ops import Runtime
from mcp_server_polymarket.prompts import register_prompts
from mcp_server_polymarket.resources import register_resources
from mcp_server_polymarket.tools import register_tools


def detect_capabilities(settings: Settings) -> Capabilities:
    # A throwaway runtime: capabilities depend only on configuration and stored credentials.
    return Runtime.create(settings).caps


def create_server(
    settings: Settings | None = None, *, transport: httpx2.AsyncBaseTransport | None = None
) -> MCPServer:
    resolved = settings or Settings()
    caps = detect_capabilities(resolved)
    running: list[Runtime] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[Runtime]:
        runtime = Runtime.create(resolved, transport=transport, caps=caps)
        running.append(runtime)
        try:
            yield runtime
        finally:
            running.remove(runtime)
            await runtime.aclose()

    server = MCPServer(
        "mcp-server-polymarket",
        title="Polymarket (unofficial)",
        version=__version__,
        instructions=knowledge.agent_guide(caps.toolsets, gated=bool(caps.gates)),
        lifespan=lifespan,
        log_level=resolved.log_level,
    )
    configure_logging(resolved.log_level)
    register_tools(server, caps)
    register_resources(server, lambda: running[-1])
    register_prompts(server, caps)
    return server


def __getattr__(name: str) -> Any:
    # `mcp dev` and `mcp run` look for a module-level `server`.
    if name == "server":
        return create_server()
    raise AttributeError(name)
