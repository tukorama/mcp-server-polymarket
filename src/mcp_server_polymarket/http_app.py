import hmac

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import ConfigError

# The SDK only applies its DNS-rebinding checks to these exact names.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers", [])).get(b"authorization", b"")
        if hmac.compare_digest(header, self.expected):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)


def build_http_app(server: MCPServer, settings: Settings, *, host: str) -> ASGIApp:
    token = settings.http_bearer_token.get_secret_value() if settings.http_bearer_token else None
    loopback = host in _LOOPBACK_HOSTS
    if not loopback and not token:
        raise ConfigError(
            f"Refusing to serve on {host} without authentication.",
            hint="Set POLYMARKET_HTTP_BEARER_TOKEN, or bind to 127.0.0.1.",
        )

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    server.custom_route("/health", methods=["GET"])(health)
    security = (
        None if loopback else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    app = server.streamable_http_app(host=host, transport_security=security)
    return BearerAuth(app, token) if token else app
