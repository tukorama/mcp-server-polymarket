import json
import os
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest

from mcp_server_polymarket import wallet_tx
from mcp_server_polymarket.client import base, cache
from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"

# Every endpoint any test reached, for the coverage check at the end of a full run.
SEEN: set[str] = set()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeApi:
    """Scripted Polymarket services. Routes are endpoints; the last response repeats."""

    def __init__(self, settings: Settings) -> None:
        self.hosts = {
            urlsplit(settings.url(service.value)).hostname: service for service in ep.Service
        }
        self.routes: dict[tuple[str, str | None], deque[tuple[int, Any, dict[str, str]]]] = {}
        self.requests: list[tuple[ep.Endpoint | None, httpx2.Request]] = []
        self.transport = httpx2.MockTransport(self._handle)

    def add(self, endpoint: ep.Endpoint, *responses: Any, path: str | None = None) -> None:
        """Script `endpoint`. `path` pins one concrete path of a templated route."""
        normalized = []
        for response in responses:
            if isinstance(response, tuple):
                status, body, *rest = response
            else:
                status, body, rest = 200, response, []
            normalized.append((status, body, rest[0] if rest else {}))
        self.routes[(endpoint.key, path)] = deque(normalized)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        service = self.hosts.get(request.url.host)
        endpoint = ep.find(service, request.method, request.url.path) if service else None
        self.requests.append((endpoint, request))
        if endpoint is None:
            return httpx2.Response(404, json={"error": f"unscripted {request.url}"})
        SEEN.add(endpoint.key)
        queue = self.routes.get((endpoint.key, request.url.path)) or self.routes.get(
            (endpoint.key, None)
        )
        if not queue:
            return httpx2.Response(404, json={"error": f"unscripted {endpoint.key}"})
        status, body, headers = queue.popleft() if len(queue) > 1 else queue[0]
        if callable(body):
            body = body(request)
        if isinstance(body, httpx2.Response):
            return body
        if isinstance(body, str | bytes):
            return httpx2.Response(status, content=body, headers=headers)
        if body is None:
            return httpx2.Response(status, headers=headers)
        return httpx2.Response(status, json=body, headers=headers)

    def calls(self, endpoint: ep.Endpoint) -> list[httpx2.Request]:
        return [request for seen, request in self.requests if seen == endpoint]

    def body(self, endpoint: ep.Endpoint, index: int = -1) -> Any:
        return json.loads(self.calls(endpoint)[index].content)

    def query(self, endpoint: ep.Endpoint, index: int = -1) -> dict[str, str]:
        return dict(self.calls(endpoint)[index].url.params)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = 1_794_000_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall + (self.now - 1000.0)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(base, "_sleep", fake.sleep)
    monkeypatch.setattr(base, "_monotonic", fake.monotonic)
    monkeypatch.setattr(base, "_now", fake.time)
    monkeypatch.setattr(cache, "_now", fake.time)
    monkeypatch.setattr(wallet_tx, "_sleep", fake.sleep)
    monkeypatch.setattr(wallet_tx, "_now", fake.time)
    monkeypatch.setattr(wallet_tx, "_monotonic", fake.monotonic)
    return fake


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A developer's real keys and gates must never reach a test.
    for name in list(os.environ):
        if name.startswith("POLYMARKET_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "xdg-cache"))


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        session_file=tmp_path / "config" / "session.json",
        perps_session_file=tmp_path / "config" / "perps_session.json",
        journal_file=tmp_path / "config" / "journal.sqlite3",
        cache_dir=tmp_path / "cache",
        **overrides,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def api(settings: Settings) -> FakeApi:
    return FakeApi(settings)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    strict = os.environ.get("POLYMARKET_TEST_COVERAGE") == "strict"
    if not strict or session.testsfailed:
        return
    unseen = sorted(endpoint.key for endpoint in ep.ALL if endpoint.key not in SEEN)
    if unseen:
        print("\nEndpoints no test reached:\n  " + "\n  ".join(unseen))
        session.exitstatus = 1
