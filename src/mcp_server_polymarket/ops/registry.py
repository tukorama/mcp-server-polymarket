import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypeVar

import httpx2
from pydantic import Field

from mcp_server_polymarket.capabilities import Capabilities, visible
from mcp_server_polymarket.client.base import ApiClient
from mcp_server_polymarket.client.cache import JsonCache
from mcp_server_polymarket.client.session import CredentialStore
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.identity import Identity

Report = Callable[[float, str], Awaitable[None]]
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
Gate = Literal["cancel", "trading", "transfers"]

# Shared parameter types. Tools use the same names for the same things across products.
TokenId = Annotated[
    str,
    Field(min_length=1, description="Outcome token id (the CLOB asset id), a long decimal string."),
]
ConditionId = Annotated[
    str, Field(pattern=r"^0x[0-9a-fA-F]{64}$", description="Market condition id, 0x + 64 hex.")
]
Wallet = Annotated[
    str,
    Field(pattern=r"^0x[0-9a-fA-F]{40}$", description="Polymarket wallet (proxy) address."),
]
Limit = Annotated[int, Field(ge=1, le=100, description="Rows per page.")]
Cursor = Annotated[
    str | None, Field(description="next_cursor from the previous page. Empty starts at the top.")
]
InstrumentId = Annotated[int, Field(ge=1, description="Perps instrument id, e.g. 1.")]
When = Annotated[
    str | None,
    Field(description="ISO 8601 time, e.g. 2026-11-01 or 2026-11-01T12:00:00Z, or epoch seconds."),
]


async def _no_report(_fraction: float, _message: str) -> None:
    return None


@dataclass
class Runtime:
    settings: Settings
    identity: Identity
    client: ApiClient
    cache: JsonCache
    caps: Capabilities
    _journal: Any = None
    # (monotonic time, response) of the last geoblock check; see safety.check_geoblock.
    geoblock: tuple[float, dict[str, Any]] | None = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        ws_transport: httpx2.AsyncBaseTransport | None = None,
        caps: Capabilities | None = None,
    ) -> "Runtime":
        settings.register_secrets()
        store = CredentialStore(
            settings.resolved_session_file, settings.resolved_perps_session_file
        )
        identity = Identity(settings, store)
        return cls(
            settings=settings,
            identity=identity,
            client=ApiClient(settings, identity, transport=transport, ws_transport=ws_transport),
            cache=JsonCache(settings.resolved_cache_dir),
            caps=caps or Capabilities.detect(settings, identity),
        )

    @property
    def journal(self) -> Any:
        if self._journal is None:
            from mcp_server_polymarket.journal import Journal

            self._journal = Journal(self.settings.resolved_journal_file)
        return self._journal

    async def aclose(self) -> None:
        await self.client.aclose()
        if self._journal is not None:
            self._journal.close()


@dataclass
class Call:
    """What an operation sees of the current invocation."""

    runtime: Runtime
    report: Report = _no_report
    wait_cap: float | None = None
    started: float = field(default_factory=time.monotonic)
    _last_fraction: float = 0.0

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    @property
    def client(self) -> ApiClient:
        return self.runtime.client

    @property
    def identity(self) -> Identity:
        return self.runtime.identity

    @property
    def deadline(self) -> float | None:
        """Absolute monotonic time this call must finish by, if the client limits it."""
        return None if self.wait_cap is None else self.started + self.wait_cap

    def limit_wait(self, seconds: float) -> float:
        """Shrink a wait to what is left of this call's time allowance."""
        if self.wait_cap is None:
            return seconds
        return min(seconds, max(self.wait_cap - (time.monotonic() - self.started), 0.0))

    async def json(self, endpoint: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("deadline", self.deadline)
        return await self.client.json(endpoint, **kwargs)

    async def request(self, endpoint: Any, **kwargs: Any) -> httpx2.Response:
        kwargs.setdefault("deadline", self.deadline)
        return await self.client.request(endpoint, **kwargs)

    async def progress(self, fraction: float, message: str) -> None:
        # MCP requires strictly increasing progress values.
        last = self._last_fraction
        value = fraction if last < fraction < 1 else last + (1 - last) * 0.01
        self._last_fraction = value
        await self.report(value, message)


Describe = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class Operation:
    fn: Callable[..., Awaitable[Any]]
    name: str
    title: str
    toolset: str
    requires: tuple[str, ...]
    gate: Gate | None
    read_only: bool
    idempotent: bool
    destructive: bool
    experimental: bool
    # Builds the text the user confirms. Present on every tool that needs confirmation.
    describe: Describe | None

    @property
    def description(self) -> str:
        text = (self.fn.__doc__ or "").strip()
        if self.experimental:
            text += (
                "\n\nExperimental: the wire format is taken from the documentation and has not "
                "been verified against the live API by this project."
            )
        return text

    @property
    def needs_confirmation(self) -> bool:
        return self.describe is not None


OPERATIONS: list[Operation] = []


def operation(
    title: str,
    *,
    toolset: str,
    requires: tuple[str, ...] = (),
    gate: Gate | None = None,
    read_only: bool = False,
    idempotent: bool = False,
    destructive: bool = False,
    experimental: bool = False,
    describe: Describe | None = None,
) -> Callable[[F], F]:
    def register(fn: F) -> F:
        if any(op.name == fn.__name__ for op in OPERATIONS):
            raise RuntimeError(f"duplicate operation {fn.__name__}")
        OPERATIONS.append(
            Operation(
                fn=fn,
                name=fn.__name__,
                title=title,
                toolset=toolset,
                requires=requires,
                gate=gate,
                read_only=read_only,
                idempotent=idempotent or read_only,
                destructive=destructive,
                experimental=experimental,
                describe=describe,
            )
        )
        return fn

    return register


def confirmation_gate(op: Operation, runtime: Runtime, kwargs: dict[str, Any]) -> Gate | None:
    """The gate that decides how a call is confirmed. A resend counts as what it resends."""
    if op.name == "resend_write":
        entry = runtime.journal.get(str(kwargs.get("request_id") or ""))
        if entry is not None and entry.tool.startswith("transfers"):
            return "transfers"
    return op.gate


def enabled_operations(caps: Capabilities) -> list[Operation]:
    return [op for op in OPERATIONS if visible(op.toolset, op.requires, op.gate, caps)[0]]


def hidden_operations(caps: Capabilities) -> list[tuple[Operation, str]]:
    hidden = []
    for op in OPERATIONS:
        shown, reason = visible(op.toolset, op.requires, op.gate, caps)
        if not shown:
            hidden.append((op, reason))
    return hidden
