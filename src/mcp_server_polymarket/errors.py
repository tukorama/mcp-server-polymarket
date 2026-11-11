from typing import Any


class PolymarketError(Exception):
    """Base class. `hint` tells an agent what to do next."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.hint = hint

    def for_agent(self) -> str:
        return f"{self.message} Next step: {self.hint}" if self.hint else self.message


class ConfigError(PolymarketError):
    pass


class AuthError(PolymarketError):
    pass


class RateLimited(PolymarketError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        kwargs.setdefault(
            "hint",
            f"Wait about {int(retry_after)} seconds and retry." if retry_after else "Retry later.",
        )
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class PermissionDenied(PolymarketError):
    pass


class InvalidRequest(PolymarketError):
    """Polymarket rejected the parameters (a 4xx other than auth, rate limit or not found)."""


class NotFound(PolymarketError):
    pass


class ServerError(PolymarketError):
    pass


class NetworkError(PolymarketError):
    """The request never produced a response."""


class DeadlineExceeded(PolymarketError):
    """The call ran out of the time the client allows a tool."""


class Refused(PolymarketError):
    """This server refused to act, before anything was sent to Polymarket."""


class SigningUnavailable(ConfigError):
    def __init__(self) -> None:
        super().__init__(
            "Signing needs the optional `signing` extra.",
            hint='Install it with `uv tool install "mcp-server-polymarket[signing]"` or run '
            '`uvx --from "mcp-server-polymarket[signing]" mcp-server-polymarket`.',
        )
