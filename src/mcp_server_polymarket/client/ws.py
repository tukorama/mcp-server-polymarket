"""Short WebSocket captures: connect, subscribe, collect for a while, disconnect.

Tools return a snapshot of what arrived, because an MCP tool call cannot stream for long.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from httpx2.websockets import WebSocketDisconnect, WebSocketNetworkError, WebSocketUpgradeError

from mcp_server_polymarket.errors import NetworkError

logger = logging.getLogger(__name__)

_monotonic = time.monotonic


@dataclass
class Heartbeat:
    """What to send, and how often, to keep a socket open."""

    every: float
    message: str


@dataclass
class Capture:
    events: list[Any] = field(default_factory=list)
    replies: list[Any] = field(default_factory=list)
    seconds: float = 0.0
    closed: str | None = None
    truncated: bool = False


Reply = Callable[[str], str | None]


def _decode(text: str) -> list[Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return [text]
    return value if isinstance(value, list) else [value]


async def collect(
    client: httpx2.AsyncClient,
    url: str,
    *,
    send: list[Any],
    seconds: float,
    max_events: int,
    heartbeat: Heartbeat | None = None,
    reply: Reply | None = None,
    is_reply: Callable[[Any], bool] | None = None,
) -> Capture:
    """Collect up to `max_events` messages or for `seconds`, whichever comes first.

    `reply` answers server pings (return the text to send). Messages for which `is_reply` is
    true, such as subscription acknowledgements, are kept apart from events.
    """
    capture = Capture()
    started = _monotonic()
    try:
        await _run(
            client, url, capture, started, send, seconds, max_events, heartbeat, reply, is_reply
        )
    except Exception as exc:
        failure = _leaf(exc)
        if isinstance(failure, WebSocketDisconnect):
            reason = f": {failure.reason}" if failure.reason else ""
            capture.closed = f"closed by the server ({failure.code}{reason})"
        elif isinstance(failure, WebSocketUpgradeError):
            status = getattr(failure.response, "status_code", None)
            raise NetworkError(
                f"The WebSocket at {url} refused the connection (HTTP {status}).",
                hint="Check credentials and the subscription parameters.",
            ) from exc
        elif isinstance(failure, WebSocketNetworkError | httpx2.TransportError | OSError):
            if not capture.events:
                raise NetworkError(
                    f"Could not stream from {url} ({type(failure).__name__}).",
                    hint="The host may be unreachable from this network. Retry later.",
                ) from exc
            capture.closed = f"connection lost ({type(failure).__name__})"
        else:
            raise
    capture.seconds = round(_monotonic() - started, 2)
    return capture


def _leaf(exc: BaseException) -> BaseException:
    """The WebSocket session reports failures from its own task group, wrapped in groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _run(
    client: httpx2.AsyncClient,
    url: str,
    capture: Capture,
    started: float,
    send: list[Any],
    seconds: float,
    max_events: int,
    heartbeat: Heartbeat | None,
    reply: Reply | None,
    is_reply: Callable[[Any], bool] | None,
) -> None:
    async with client.websocket(url, keepalive_ping_interval_seconds=20) as ws:
        for message in send:
            await ws.send_text(message if isinstance(message, str) else json.dumps(message))
        last_beat = _monotonic()
        while True:
            elapsed = _monotonic() - started
            if elapsed >= seconds or len(capture.events) >= max_events:
                capture.truncated = len(capture.events) >= max_events
                return
            wait = seconds - elapsed
            if heartbeat is not None:
                wait = min(wait, max(heartbeat.every - (_monotonic() - last_beat), 0.05))
            try:
                text = await ws.receive_text(timeout=wait)
            except TimeoutError:
                text = None
            if heartbeat is not None and _monotonic() - last_beat >= heartbeat.every:
                await ws.send_text(heartbeat.message)
                last_beat = _monotonic()
            if text is None:
                continue
            answer = reply(text) if reply else None
            if answer is not None:
                await ws.send_text(answer)
                continue
            if text in ("PONG", "pong", ""):
                continue
            for item in _decode(text):
                if is_reply and is_reply(item):
                    capture.replies.append(item)
                else:
                    capture.events.append(item)
