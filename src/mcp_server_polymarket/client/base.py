import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, cast

import httpx2

from mcp_server_polymarket import __version__
from mcp_server_polymarket.client import auth
from mcp_server_polymarket.client.endpoints import Auth, Endpoint, Service
from mcp_server_polymarket.client.retry import (
    RETRY_SAFE,
    RETRY_UNSAFE,
    backoff,
    clamp,
    parse_retry_after,
)
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import (
    AuthError,
    DeadlineExceeded,
    InvalidRequest,
    NetworkError,
    NotFound,
    NotSent,
    PermissionDenied,
    PolymarketError,
    RateLimited,
    ServerError,
)
from mcp_server_polymarket.identity import Identity

logger = logging.getLogger(__name__)

_sleep = asyncio.sleep
_monotonic = time.monotonic
_now = time.time

Params = Mapping[str, Any] | None


def parse_json(response: httpx2.Response) -> Any:
    if not response.content.strip():
        return None
    try:
        return response.json()
    except ValueError:
        return None


def encode_body(body: Any) -> bytes:
    """The one serialization of a request body. Signatures cover these exact bytes."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def _query(params: Params) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        values = value if isinstance(value, list | tuple) else [value]
        for item in values:
            if isinstance(item, bool):
                pairs.append((key, "true" if item else "false"))
            else:
                pairs.append((key, str(item)))
    return pairs


def _message(body: Any, text: str) -> str:
    if isinstance(body, dict):
        for key in ("error", "errorMsg", "error_msg", "message", "detail", "msg"):
            value = body.get(key)
            if value:
                return str(value)
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return _message(body[0], text)
    return text[:300] or "no details"


class ApiClient:
    """One HTTP client for every Polymarket service. Retries reads, never ambiguous writes."""

    def __init__(
        self,
        settings: Settings,
        identity: Identity,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        ws_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.transport = transport
        self.ws_transport = ws_transport
        self._ws: httpx2.AsyncClient | None = None
        self._http = httpx2.AsyncClient(
            follow_redirects=False,
            timeout=settings.http_timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": f"mcp-server-polymarket/{__version__}",
            },
        )

    @property
    def http(self) -> httpx2.AsyncClient:
        return self._http

    @property
    def ws(self) -> httpx2.AsyncClient:
        """A separate client for WebSockets, so tests can give it an ASGI transport."""
        if self._ws is None:
            self._ws = httpx2.AsyncClient(
                timeout=self.settings.http_timeout,
                transport=self.ws_transport,
                headers={"User-Agent": f"mcp-server-polymarket/{__version__}"},
            )
        return self._ws

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._ws is not None:
            await self._ws.aclose()

    def base_url(self, service: Service) -> str:
        return self.settings.url(service.value)

    def _auth_headers(self, endpoint: Endpoint, path: str, body: bytes | None) -> dict[str, str]:
        kind = endpoint.auth
        identity = self.identity
        if kind in (Auth.NONE, Auth.SIGNED, Auth.CLOB_L1):
            return {}
        if kind in (Auth.CLOB_L2, Auth.BUILDER):
            credentials = identity.clob_credentials()
            if credentials is None:
                raise AuthError(
                    "This needs CLOB API credentials, and none are configured.",
                    hint="Run `mcp-server-polymarket auth` once, or set POLYMARKET_API_KEY, "
                    "POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE and POLYMARKET_ADDRESS.",
                )
            key = auth.HmacKey(credentials.api_key, credentials.secret, credentials.passphrase)
            headers = auth.clob_l2_headers(
                key, credentials.address, int(_now()), endpoint.method, path, body
            )
            if kind == Auth.BUILDER:
                builder = identity.builder_key()
                if builder is None:
                    raise AuthError("This needs builder API credentials (POLYMARKET_BUILDER_*).")
                headers |= auth.builder_headers(builder, int(_now()), endpoint.method, path, body)
            return headers
        if kind == Auth.RELAYER:
            relayer = identity.relayer_key()
            if relayer is not None:
                return auth.relayer_headers(*relayer)
            builder = identity.builder_key()
            if builder is not None:
                return auth.builder_headers(builder, int(_now()), endpoint.method, path, body)
            raise AuthError(
                "This needs a Relayer API key, and none is configured.",
                hint="Create one at polymarket.com under Settings, API Keys, then set "
                "POLYMARKET_RELAYER_API_KEY and POLYMARKET_RELAYER_API_KEY_ADDRESS.",
            )
        session = identity.perps_session()
        if session is None:
            if kind == Auth.PERPS_OPTIONAL:
                return {}
            raise AuthError(
                "This needs a Perps session, and none is configured.",
                hint="Run `mcp-server-polymarket auth --perps`, or set POLYMARKET_PERPS_PROXY "
                "and POLYMARKET_PERPS_SECRET.",
            )
        return auth.perps_headers(session.proxy, session.secret)

    async def request(
        self,
        endpoint: Endpoint,
        *,
        path_args: dict[str, str] | None = None,
        params: Params = None,
        body: Any = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        deadline: float | None = None,
        expect: tuple[int, ...] = (),
    ) -> httpx2.Response:
        """Send one request. `expect` lists 4xx statuses returned instead of raised."""
        path = endpoint.render(path_args)
        url = self.base_url(endpoint.service) + path
        content = (
            raw_body if raw_body is not None else (None if body is None else encode_body(body))
        )
        policy = RETRY_SAFE if endpoint.safe_to_retry else RETRY_UNSAFE
        query = _query(params)
        rate_limited = server_failures = network_failures = 0
        label = f"{endpoint.method} {endpoint.service.name.lower()}{endpoint.path}"
        # Whether some attempt may have reached Polymarket. A write that never did, did not happen.
        maybe_sent = False

        async def pause(seconds: float) -> None:
            # A retry must not outlive the caller's time allowance, or the client cancels the call.
            if deadline is not None and _monotonic() + seconds > deadline:
                if not maybe_sent:
                    raise NotSent(
                        f"{label} was not sent in the time this tool call allows.",
                        hint="Nothing was sent. Try again in a moment.",
                    )
                raise DeadlineExceeded(
                    f"{label} did not answer in the time this tool call allows.",
                    hint="Try again in a moment.",
                )
            await _sleep(seconds)

        while True:
            request_headers = dict(headers or {})
            request_headers |= self._auth_headers(endpoint, path, content)
            if content is not None:
                request_headers.setdefault("Content-Type", "application/json")
            timeout: float = self.settings.http_timeout
            if deadline is not None:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    if not maybe_sent:
                        raise NotSent(f"No time left to send {label}.", hint="Try again.")
                    raise DeadlineExceeded(f"No time left for {label}.", hint="Try again.")
                timeout = max(min(timeout, remaining), 0.5)
            try:
                response = await self._http.request(
                    endpoint.method,
                    url,
                    params=cast(Any, query) or None,
                    content=content,
                    headers=request_headers,
                    timeout=timeout,
                )
            except (httpx2.TransportError, httpx2.DecodingError) as exc:
                reached = not isinstance(exc, httpx2.ConnectError | httpx2.ConnectTimeout)
                maybe_sent = maybe_sent or reached
                network_failures += 1
                if network_failures >= policy.network_attempts or (
                    reached and not policy.retry_ambiguous_timeouts
                ):
                    if reached and not endpoint.safe_to_retry:
                        raise NetworkError(
                            f"{label} failed after it may have reached Polymarket "
                            f"({type(exc).__name__}).",
                            hint="The outcome is unknown. Check before repeating this write.",
                        ) from exc
                    raise (NetworkError if maybe_sent else NotSent)(
                        f"Could not reach {endpoint.service.name.lower()} for {label} "
                        f"({type(exc).__name__}).",
                        hint="The host may be unreachable from this network. Retry later.",
                    ) from exc
                await pause(backoff(network_failures))
                continue

            maybe_sent = True
            status = response.status_code
            retry_after = parse_retry_after(response)
            if status in (425, 429):
                rate_limited += 1
                if rate_limited >= policy.rate_limit_attempts or not endpoint.safe_to_retry:
                    raise RateLimited(
                        f"Polymarket is throttling {label}"
                        + (" while its matching engine restarts." if status == 425 else "."),
                        retry_after=retry_after,
                        status=status,
                        detail=parse_json(response),
                    )
                await pause(clamp(retry_after, 1, 30) if retry_after else backoff(rate_limited))
                continue
            if status in (500, 502, 503, 504):
                server_failures += 1
                if server_failures >= policy.server_attempts:
                    self._raise_for_status(label, response, endpoint)
                await pause(
                    clamp(retry_after, 0.5, 15) if retry_after else backoff(server_failures)
                )
                continue
            # Redirects are not followed, so a 3xx means the request did not do its job; for a
            # write that is an unknown outcome, never a success.
            if status >= 300 and status not in expect:
                self._raise_for_status(label, response, endpoint)
            return response

    @staticmethod
    def _raise_for_status(label: str, response: httpx2.Response, endpoint: Endpoint) -> None:
        status = response.status_code
        body = parse_json(response)
        message = _message(body, response.text)
        if status == 401:
            raise AuthError(
                f"Polymarket rejected the credentials for {label}: {message}",
                status=status,
                detail=body,
                hint="Check the configured credentials, or run `mcp-server-polymarket auth`.",
            )
        if status == 403:
            raise PermissionDenied(
                f"Polymarket refused {label}: {message}",
                status=status,
                detail=body,
                hint="The account or region may not be allowed to do this.",
            )
        if status == 404:
            raise NotFound(f"Not found: {label} ({message})", status=status, detail=body)
        if 400 <= status < 500:
            raise InvalidRequest(
                f"Polymarket rejected {label}: {message}", status=status, detail=body
            )
        writes = not endpoint.safe_to_retry
        raise ServerError(
            f"{label} returned {status}: {message}",
            status=status,
            detail=body,
            hint="The outcome is unknown. Check before repeating this write."
            if writes
            else "Retry later.",
        )

    async def json(self, endpoint: Endpoint, **kwargs: Any) -> Any:
        return parse_json(await self.request(endpoint, **kwargs))


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, PolymarketError):
        return exc.for_agent()
    return f"{type(exc).__name__}: {exc}"
