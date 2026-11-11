"""Use Polymarket from LangChain or LangGraph directly, without an MCP subprocess.

Needs the extra: `uv add "mcp-server-polymarket[langgraph]"`.

Tools that place orders or move funds pause the graph with `interrupt()` and show the user the
same text the MCP server would ask them to confirm. Resume with `Command(resume=True)` to go
ahead, or anything else to decline. That needs a checkpointer, which `create_polymarket_agent`
provides.
"""

import asyncio
import inspect
import threading
import typing
from collections.abc import Awaitable, Coroutine, Iterable
from typing import Any, TypeVar, cast

from langchain.agents import create_agent
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from pydantic import BaseModel, create_model

from mcp_server_polymarket import knowledge
from mcp_server_polymarket.capabilities import Capabilities
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import PolymarketError
from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops import OPERATIONS, Call, Operation, Runtime, enabled_operations
from mcp_server_polymarket.ops.registry import confirmation_gate
from mcp_server_polymarket.redaction import scrub
from mcp_server_polymarket.server import detect_capabilities

T = TypeVar("T")

DECLINED = "The user did not confirm, so nothing was sent."


def agent_prompt(caps: Capabilities) -> str:
    return knowledge.agent_guide(caps.toolsets, gated=bool(caps.gates))


class _Bridge:
    """Runs every call on one background event loop.

    One loop means one HTTP client, one journal and one relayer lock per wallet, whether
    LangGraph invokes the tools from async code or from worker threads.
    """

    def __init__(self, settings: Settings, caps: Capabilities) -> None:
        self.settings = settings
        self.caps = caps
        self._loop = asyncio.new_event_loop()
        self._runtime: Runtime | None = None
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def _call_of(self) -> Call:
        if self._runtime is None:
            self._runtime = Runtime.create(self.settings, caps=self.caps)
        return Call(runtime=self._runtime)

    async def _guard(self, coroutine: Awaitable[T]) -> T:
        try:
            return await coroutine
        except PolymarketError as exc:
            raise ToolException(scrub(exc.for_agent())) from exc
        except Exception as exc:
            raise ToolException(scrub(f"Internal error: {type(exc).__name__}: {exc}")) from exc

    async def run(self, op: Operation, kwargs: dict[str, Any]) -> Any:
        result = await self._guard(op.fn(self._call_of(), **kwargs))
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

    async def needs_asking(self, op: Operation, kwargs: dict[str, Any]) -> bool:
        if not op.needs_confirmation or kwargs.get("dry_run"):
            return False
        gate = confirmation_gate(op, self._call_of().runtime, kwargs)
        return self.settings.confirm != "none" or gate == "transfers"

    async def describe(self, op: Operation, kwargs: dict[str, Any]) -> str:
        assert op.describe is not None
        return scrub(await self._guard(op.describe(self._call_of(), **kwargs)))

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)


def _args_schema(op: Operation) -> Any:
    signature = inspect.signature(op.fn, eval_str=True)
    hints = typing.get_type_hints(op.fn, include_extras=True)
    fields: dict[str, Any] = {
        param.name: (
            hints.get(param.name, Any),
            ... if param.default is inspect.Parameter.empty else param.default,
        )
        for param in list(signature.parameters.values())[1:]
    }
    return create_model(f"{op.name}_input", **fields)


def approved(answer: Any) -> bool:
    """Whether a resume value means yes. Anything unclear is a no."""
    if answer is True:
        return True
    if isinstance(answer, str):
        return answer.strip().lower() in ("y", "yes", "approve", "approved", "confirm")
    if isinstance(answer, dict):
        answer = cast(dict[str, Any], answer)
        if answer.get("type") in ("approve", "accept"):
            return True
        return answer.get("approve") is True or answer.get("confirm") is True
    return False


def _declined(op: Operation) -> Any:
    result_type = typing.get_type_hints(op.fn).get("return", WriteResult)
    result = cast(type[WriteResult], result_type)(
        status="declined", message=DECLINED, next_step="Do not retry unless the user asks again."
    )
    return result.model_dump(mode="json")


def _approval(op: Operation, text: str) -> dict[str, Any]:
    return {"tool": op.name, "message": text, "gate": op.gate}


def _build_tool(op: Operation, bridge: _Bridge, prefix: str) -> BaseTool:
    # interrupt() must run in the graph's own context, so the question is asked here and only the
    # API work goes to the bridge loop.
    def run(**kwargs: Any) -> Any:
        if bridge.submit(bridge.needs_asking(op, kwargs)).result():
            text = bridge.submit(bridge.describe(op, kwargs)).result()
            if not approved(interrupt(_approval(op, text))):
                return _declined(op)
        return bridge.submit(bridge.run(op, kwargs)).result()

    async def arun(**kwargs: Any) -> Any:
        if await asyncio.wrap_future(bridge.submit(bridge.needs_asking(op, kwargs))):
            text = await asyncio.wrap_future(bridge.submit(bridge.describe(op, kwargs)))
            if not approved(interrupt(_approval(op, text))):
                return _declined(op)
        return await asyncio.wrap_future(bridge.submit(bridge.run(op, kwargs)))

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=f"{prefix}{op.name}",
        description=op.description,
        args_schema=_args_schema(op),
        handle_tool_error=True,
        metadata={"toolset": op.toolset, "gate": op.gate, "read_only": op.read_only},
    )


def _resolve(settings: Settings | None, toolsets: str | None) -> Settings:
    resolved = settings or Settings()
    if toolsets is not None:
        resolved = resolved.model_copy(update={"toolsets": toolsets})
    return resolved


def get_polymarket_tools(
    settings: Settings | None = None,
    *,
    toolsets: str | None = None,
    prefix: str = "",
    include_gated: bool = False,
) -> list[BaseTool]:
    """Return LangChain tools for the operations this configuration exposes.

    `toolsets` overrides POLYMARKET_TOOLSETS (same syntax, e.g. "auto,+sports"). Tools that need
    the user's confirmation (orders, transfers, wallet transactions) call `interrupt()`, so they
    only work inside a graph with a checkpointer; they are left out unless `include_gated`.
    Cancels need no confirmation and are included whenever POLYMARKET_ALLOW_CANCEL is on.
    """
    resolved = _resolve(settings, toolsets)
    caps = detect_capabilities(resolved)
    bridge = _Bridge(resolved, caps)
    return [
        _build_tool(op, bridge, prefix)
        for op in enabled_operations(caps)
        if include_gated or not op.needs_confirmation
    ]


def create_polymarket_agent(
    model: Any,
    *,
    settings: Settings | None = None,
    toolsets: str | None = None,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    middleware: list[Any] | None = None,
    checkpointer: Any = "memory",
) -> Any:
    """Build a LangGraph agent wired to Polymarket.

    When trading or transfers are enabled, each confirmed write pauses the graph with the text the
    user must approve; resume with `Command(resume=True)` from `langgraph.types`, or any other
    value to decline. Approvals need a checkpointer: the default keeps them in memory, or pass
    your own saver, or None if LangGraph Platform supplies one.
    """
    resolved = _resolve(settings, toolsets)
    caps = detect_capabilities(resolved)
    own = get_polymarket_tools(resolved, include_gated=True)
    gated = any(op.needs_confirmation for op in enabled_operations(caps))
    if checkpointer == "memory":
        checkpointer = InMemorySaver() if gated else None
    return create_agent(
        model,
        [*own, *(tools or [])],
        system_prompt=system_prompt or agent_prompt(caps),
        middleware=list(middleware or []),
        checkpointer=checkpointer,
    )


_ACCESS = {
    "read": "read-only",
    "write": "changes account state",
    "funds": "places orders or moves funds, asks first",
}
_FLOWCHART = """\
---
config:
  flowchart:
    nodeSpacing: 24
    rankSpacing: 12
    padding: 8
---
flowchart TB
"""
# Fills are translucent and text has no color, so the diagram reads on light and dark pages.
_STYLES = [
    "classDef default fill:#64748b,fill-opacity:0.06,stroke:#64748b",
    "classDef read fill:#64748b,fill-opacity:0.14,stroke:#64748b",
    "classDef write fill:#d97706,fill-opacity:0.16,stroke:#d97706",
    "classDef funds fill:#dc2626,fill-opacity:0.16,stroke:#dc2626,stroke-width:2px",
    "classDef external fill-opacity:0,stroke-dasharray:5 4",
    "classDef box fill:#64748b,fill-opacity:0.05,stroke:#64748b,stroke-opacity:0.45",
    "classDef row fill:none,stroke:none",
]


def _access(op: Operation) -> str:
    if op.needs_confirmation:
        return "funds"
    return "read" if op.read_only else "write"


def _subgraph(node_id: str, title: str, direction: str, body: list[str]) -> list[str]:
    return [
        f'subgraph {node_id}["{title}"]',
        f"    direction {direction}",
        *(f"    {line}" for line in body),
        "end",
    ]


def _in_order(ids: list[str]) -> list[str]:
    # Mermaid versions disagree on how to order unlinked subgraphs, so invisible links pin it.
    return [" ~~~ ".join(ids)] if len(ids) > 1 else []


def _graph_lines(drawn: Any) -> list[str]:
    # Numbered ids keep node names clear of Mermaid keywords and of the ids used around them.
    ids = {name: f"n{index}" for index, name in enumerate(drawn.nodes)}
    lines: list[str] = []
    for name in drawn.nodes:
        if name in ("__start__", "__end__"):
            lines.append(f"{ids[name]}([{name.strip('_')}])")
        else:
            lines.append(f'{ids[name]}["{name}"]')
    for edge in drawn.edges:
        arrow = "-.->" if edge.conditional else "-->"
        link = arrow if edge.data is None else f'{arrow}|"{edge.data}"|'
        lines.append(f"{ids[edge.source]} {link} {ids[edge.target]}")
    return lines


def _tool_boxes(names: Iterable[str]) -> dict[str, list[str]]:
    """One Mermaid subgraph per toolset, holding a node per access level."""
    operations = {op.name: op for op in OPERATIONS}
    areas: dict[str, dict[str, list[str]]] = {}
    for name in names:
        op = operations.get(name)
        area = op.toolset if op else "other"
        areas.setdefault(area, {}).setdefault(_access(op) if op else "", []).append(name)
    boxes = {}
    for area, levels in areas.items():
        key = area.replace("-", "_")
        nodes = [
            f'tools_{key}_{level or "all"}["{"<br/>".join(levels[level])}"]'
            + (f":::{level}" if level else "")
            for level in (*_ACCESS, "")
            if level in levels
        ]
        boxes[f"tools_{key}"] = _subgraph(f"tools_{key}", area, "LR", nodes)
    return boxes


def draw_mermaid(graph: Any) -> str:
    """Draw a compiled graph as a Mermaid flowchart.

    The graph is drawn as LangGraph reports it. The Polymarket tools are listed under it, grouped
    by toolset and colored by what they can change.
    """
    drawn = graph.get_graph()
    operations = {op.name: op for op in OPERATIONS}
    tools = dict.fromkeys(
        name
        for node in drawn.nodes.values()
        for name in getattr(node.data, "tools_by_name", None) or ()
    )
    lines = _subgraph("agent", "Agent graph", "LR", _graph_lines(drawn))
    boxes = ["agent"]
    rows: list[str] = []
    areas = _tool_boxes(tools)
    if areas:
        ids = list(areas)
        grid: list[str] = []
        for start in range(0, len(ids), 3):
            row = ids[start : start + 3]
            rows.append(f"tools_row{start // 3}")
            body = [line for box in row for line in areas[box]]
            grid += _subgraph(rows[-1], " ", "LR", body + _in_order(row))
        lines += _subgraph("tools", "Tools", "TB", grid + _in_order(rows))
        lines.append("agent -- tool calls --> tools")
        boxes += ["tools", *ids]
    used = {_access(operations[name]) for name in tools if name in operations}
    if used:
        key = [
            f'legend_{level}["{text}"]:::{level}'
            for level, text in _ACCESS.items()
            if level in used
        ]
        lines += _subgraph("legend", "Legend", "TB", key)
        lines += [
            'polymarket["Polymarket APIs"]:::external',
            "tools -- HTTPS / WebSocket --> polymarket",
            "tools ~~~ legend",
        ]
        boxes.append("legend")
    lines += _STYLES
    lines.append(f"class {','.join(boxes)} box")
    if rows:
        lines.append(f"class {','.join(rows)} row")
    return _FLOWCHART + "".join(f"    {line}\n" for line in lines)
