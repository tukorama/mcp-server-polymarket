import argparse
import asyncio
import json
import sys

from pydantic import ValidationError

from mcp_server_polymarket import __version__
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import PolymarketError
from mcp_server_polymarket.logging_setup import configure_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-server-polymarket", description="MCP server for the Polymarket API (unofficial)."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    auth = commands.add_parser("auth", help="create and store API credentials")
    target = auth.add_mutually_exclusive_group()
    target.add_argument("--perps", action="store_true", help="create a Perps trading session")
    target.add_argument(
        "--session-key",
        metavar="ADDRESS",
        help="authorise a session key on the Deposit Wallet (needs the owner's key)",
    )
    target.add_argument(
        "--revoke-session-key", metavar="ADDRESS", help="revoke a Deposit Wallet session key"
    )
    target.add_argument(
        "--builder-key", action="store_true", help="create a builder API key (needs CLOB auth)"
    )
    auth.add_argument("--read-only", action="store_true", help="Perps: a key that cannot trade")
    auth.add_argument("--days", type=int, default=7, help="Perps: session lifetime in days")
    auth.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    logout = commands.add_parser("logout", help="forget or revoke stored credentials")
    logout.add_argument("--perps", action="store_true", help="revoke and forget the Perps session")
    logout.add_argument(
        "--revoke", action="store_true", help="also delete the CLOB API key at Polymarket"
    )
    logout.add_argument(
        "--builder-key", action="store_true", help="revoke the builder API key at Polymarket"
    )

    commands.add_parser("status", help="show credentials, gates and hidden tools")
    commands.add_parser("tools", help="list the tools this configuration exposes")
    return parser


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    from mcp_server_polymarket.server import create_server

    server = create_server(settings)
    if args.transport == "stdio":
        server.run()
        return 0
    import uvicorn

    from mcp_server_polymarket.http_app import build_http_app

    app = build_http_app(server, settings, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


def _tools(settings: Settings) -> int:
    from mcp_server_polymarket.ops import enabled_operations, hidden_operations
    from mcp_server_polymarket.server import detect_capabilities

    caps = detect_capabilities(settings)
    operations = enabled_operations(caps)
    width = max((len(op.name) for op in operations), default=10)
    for op in operations:
        flag = " (experimental)" if op.experimental else ""
        print(f"{op.name:<{width}}  [{op.toolset}] {op.title}{flag}")
    print(f"\n{len(operations)} tools.")
    hidden: dict[str, list[str]] = {}
    for op, reason in hidden_operations(caps):
        hidden.setdefault(reason, []).append(op.name)
    if hidden:
        print("\nHidden:")
        for reason, names in hidden.items():
            print(f"- {len(names)} tools: {reason}")
    return 0


async def _status(settings: Settings) -> int:
    from mcp_server_polymarket.ops import Runtime
    from mcp_server_polymarket.ops.registry import Call
    from mcp_server_polymarket.ops.system import auth_status

    runtime = Runtime.create(settings)
    try:
        status = await auth_status(Call(runtime=runtime))
    finally:
        await runtime.aclose()
    print(json.dumps(status.model_dump(mode="json"), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        # Only names and reasons: the input values could be secrets.
        for error in exc.errors():
            name = "_".join(str(part) for part in error["loc"]).upper()
            print(f"Invalid POLYMARKET_{name}: {error['msg']}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    if args.command is None:
        args = _parser().parse_args(["serve"])
    try:
        if args.command == "serve":
            return _serve(args, settings)
        if args.command == "status":
            return asyncio.run(_status(settings))
        if args.command in ("auth", "logout"):
            from mcp_server_polymarket import auth_flow

            handler = auth_flow.auth if args.command == "auth" else auth_flow.logout
            return asyncio.run(handler(args, settings))
        return _tools(settings)
    except PolymarketError as exc:
        print(exc.for_agent(), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
