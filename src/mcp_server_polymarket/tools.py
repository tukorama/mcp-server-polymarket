import inspect
import logging
import typing
from typing import Annotated, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server_polymarket.capabilities import Capabilities
from mcp_server_polymarket.errors import PolymarketError
from mcp_server_polymarket.models.base import WriteResult
from mcp_server_polymarket.ops import Call, Operation, Runtime, enabled_operations
from mcp_server_polymarket.redaction import scrub

logger = logging.getLogger(__name__)

# Claude Desktop cancels stdio tool calls after about a minute, so waits there are capped.
SHORT_CALL_CLIENTS = ("desktop", "claude-ai")
SHORT_CALL_SECONDS = 45.0


class Confirmation(BaseModel):
    confirm: bool = Field(default=False, description="Go ahead with this on Polymarket?")


def _wait_cap(ctx: Context) -> float | None:
    params = ctx.session.client_params
    if params is None:
        return None
    name = params.client_info.name.lower()
    if any(marker in name for marker in SHORT_CALL_CLIENTS):
        return SHORT_CALL_SECONDS
    return None


def runtime_of(ctx: Context) -> Runtime:
    return cast(Runtime, ctx.request_context.lifespan_context)


def make_call(ctx: Context) -> Call:
    async def report(fraction: float, message: str) -> None:
        await ctx.report_progress(fraction, 1.0, message)

    return Call(runtime=runtime_of(ctx), report=report, wait_cap=_wait_cap(ctx))


def agent_error(exc: BaseException, op_name: str) -> ToolError:
    if isinstance(exc, PolymarketError):
        return ToolError(scrub(exc.for_agent()))
    logger.exception("Unexpected failure in %s", op_name)
    return ToolError(scrub(f"Internal error: {type(exc).__name__}: {exc}"))


async def _invoke(op: Operation, ctx: Context, kwargs: dict[str, Any]) -> Any:
    try:
        return await op.fn(make_call(ctx), **kwargs)
    except ToolError:
        raise
    except Exception as exc:
        raise agent_error(exc, op.name) from exc


def refusal_without_elicitation(op: Operation) -> str:
    if op.gate == "transfers":
        return (
            f"{op.name} moves funds and needs the user's confirmation, but this MCP client cannot "
            "ask the user. Use a client that supports elicitation, or call with dry_run=true to "
            "preview."
        )
    return (
        f"{op.name} needs the user's confirmation, but this MCP client cannot ask the user. Use a "
        "client that supports elicitation, call with dry_run=true to preview, or let the user set "
        "POLYMARKET_CONFIRM=none to skip confirmation for trading."
    )


def _resolver(op: Operation, arg_names: list[str]) -> Any:
    """Asks the user before a write. Built per tool, because resolvers take arguments by name."""
    assert op.describe is not None
    describe = op.describe

    async def ask(**kwargs: Any) -> Any:
        ctx = cast(Context, kwargs.pop("ctx"))
        if kwargs.get("dry_run"):
            return Confirmation(confirm=True)
        settings = runtime_of(ctx).settings
        if settings.confirm == "none" and op.gate != "transfers":
            return Confirmation(confirm=True)
        resuming = ctx.input_responses is not None or ctx.request_state is not None
        capabilities = ctx.client_capabilities
        if not resuming and (capabilities is None or capabilities.elicitation is None):
            raise ToolError(refusal_without_elicitation(op))
        try:
            text = await describe(make_call(ctx), **kwargs)
        except Exception as exc:
            raise agent_error(exc, op.name) from exc
        return Elicit(scrub(text), Confirmation)

    returns = Elicit[Confirmation] | Confirmation
    parameters = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
    parameters += [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in arg_names]
    ask.__name__ = ask.__qualname__ = f"confirm_{op.name}"
    ask.__signature__ = inspect.Signature(parameters, return_annotation=returns)  # type: ignore[attr-defined]
    ask.__annotations__ = {"ctx": Context, "return": returns}
    return ask


def tool_function(op: Operation) -> Any:
    """The MCP-facing function: the operation's parameters, minus `call`, plus `ctx`."""
    signature = inspect.signature(op.fn, eval_str=True)
    hints = typing.get_type_hints(op.fn, include_extras=True)
    params = [
        param.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for param in list(signature.parameters.values())[1:]
    ]
    names = [param.name for param in params]
    annotations: dict[str, Any] = {name: hints[name] for name in names if name in hints}
    extra = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
    annotations["ctx"] = Context
    if op.needs_confirmation:
        returns = hints.get("return")
        if not (isinstance(returns, type) and issubclass(returns, WriteResult)):
            raise TypeError(f"{op.name} needs confirmation, so it must return a WriteResult")
        confirmed = Annotated[ElicitationResult[Confirmation], Resolve(_resolver(op, names))]
        extra.append(
            inspect.Parameter("confirmation", inspect.Parameter.KEYWORD_ONLY, annotation=confirmed)
        )
        annotations["confirmation"] = confirmed

    async def tool(ctx: Context, confirmation: Any = None, **kwargs: Any) -> Any:
        if op.needs_confirmation:
            accepted = isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm
            if not accepted:
                result_type = cast(type[WriteResult], hints["return"])
                return result_type(
                    status="declined",
                    message="The user did not confirm, so nothing was sent.",
                    next_step="Do not retry unless the user asks again.",
                )
        return await _invoke(op, ctx, kwargs)

    annotations["return"] = hints.get("return", Any)
    tool.__name__ = op.name
    tool.__qualname__ = op.name
    tool.__doc__ = op.description
    tool.__signature__ = signature.replace(parameters=[*params, *extra])  # type: ignore[attr-defined]
    tool.__annotations__ = annotations
    return tool


def register_tools(server: MCPServer, caps: Capabilities) -> list[Operation]:
    operations = enabled_operations(caps)
    for op in operations:
        server.add_tool(
            tool_function(op),
            name=op.name,
            title=op.title,
            description=op.description,
            annotations=ToolAnnotations(
                read_only_hint=op.read_only,
                destructive_hint=op.destructive,
                idempotent_hint=op.idempotent,
                open_world_hint=True,
            ),
        )
    return operations
