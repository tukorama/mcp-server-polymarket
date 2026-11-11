import os
import socket
import subprocess
import sys
import time

import httpx2
import pytest

from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import ConfigError
from mcp_server_polymarket.http_app import build_http_app
from mcp_server_polymarket.server import create_server

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream"}


def test_public_bind_without_a_token_is_refused():
    settings = Settings(_env_file=None)
    with pytest.raises(ConfigError):
        build_http_app(create_server(settings), settings, host="0.0.0.0")


@pytest.fixture
def http_server(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "HOME": str(tmp_path), "POLYMARKET_HTTP_BEARER_TOKEN": "sesame"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mcp_server_polymarket",
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx2.get(f"{base}/health").status_code == 200:
                break
        except httpx2.TransportError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("server did not start")
    yield base
    process.terminate()
    process.wait(timeout=10)


def test_bearer_token_guards_mcp_but_not_health(http_server):
    assert httpx2.get(f"{http_server}/health").json() == {"status": "ok"}
    denied = httpx2.post(f"{http_server}/mcp", json=INITIALIZE, headers=HEADERS)
    assert denied.status_code == 401
    wrong = httpx2.post(
        f"{http_server}/mcp", json=INITIALIZE, headers=HEADERS | {"authorization": "Bearer nope"}
    )
    assert wrong.status_code == 401
    allowed = httpx2.post(
        f"{http_server}/mcp", json=INITIALIZE, headers=HEADERS | {"authorization": "Bearer sesame"}
    )
    assert allowed.status_code == 200
