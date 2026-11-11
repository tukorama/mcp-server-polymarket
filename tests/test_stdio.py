import json
import os
import subprocess
import sys

ALLOWED = ("LOGNAME", "PATH", "SHELL", "TERM", "USER", "SYSTEMROOT")


def rpc(message_id, method, params=None):
    return json.dumps(
        {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
    )


def test_stdout_is_only_json_rpc_with_the_inherited_env_allowlist(tmp_path):
    env = {key: os.environ[key] for key in ALLOWED if key in os.environ}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)
    env["POLYMARKET_LOG_LEVEL"] = "DEBUG"
    initialize = {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdin = "\n".join([rpc(1, "initialize", initialize), notification, rpc(2, "tools/list")]) + "\n"
    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_server_polymarket"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        cwd=tmp_path,
    )
    process.stdin.write(stdin)
    process.stdin.flush()
    frames = []
    while len(frames) < 2:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        frames.append(json.loads(line))
    process.stdin.close()
    remainder = process.stdout.read()
    process.wait(timeout=20)
    assert all(frame["jsonrpc"] == "2.0" for frame in frames)
    assert remainder.strip() == ""
    tools = frames[1]["result"]["tools"]
    assert {"search", "get_service_status"} <= {tool["name"] for tool in tools}
    assert len(tools) == 49, "the default tools without credentials"
    assert frames[0]["result"]["serverInfo"]["name"] == "mcp-server-polymarket"
