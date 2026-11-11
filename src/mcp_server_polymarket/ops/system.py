import asyncio
import time
from typing import Any

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import describe_error
from mcp_server_polymarket.identity import signing_available
from mcp_server_polymarket.models.base import CompactModel
from mcp_server_polymarket.ops.registry import Call, hidden_operations, operation


class ServiceCheck(CompactModel):
    service: str
    ok: bool
    latency_ms: int | None = None
    detail: Any = None
    error: str | None = None


class Geoblock(CompactModel):
    blocked: bool | None = None
    country: str | None = None
    region: str | None = None
    error: str | None = None


class ServiceStatus(CompactModel):
    services: list[ServiceCheck]
    geoblock: Geoblock
    note: str | None = None


async def _check(call: Call, name: str, endpoint: ep.Endpoint) -> ServiceCheck:
    started = time.monotonic()
    try:
        body = await call.json(endpoint)
    except Exception as exc:
        return ServiceCheck(service=name, ok=False, error=describe_error(exc))
    latency = int((time.monotonic() - started) * 1000)
    detail = body
    if isinstance(body, dict) and name == "data_api":
        detail = {key: body[key] for key in ("serving", "age_seconds") if key in body} or None
    return ServiceCheck(service=name, ok=True, latency_ms=latency, detail=detail)


async def geoblock(call: Call) -> Geoblock:
    try:
        body = await call.json(ep.WEB_GEOBLOCK)
    except Exception as exc:
        return Geoblock(error=describe_error(exc))
    if not isinstance(body, dict):
        return Geoblock(error="Unexpected geoblock response.")
    return Geoblock(
        blocked=body.get("blocked"), country=body.get("country"), region=body.get("region")
    )


@operation("Check Polymarket services", toolset="system", read_only=True)
async def get_service_status(call: Call) -> ServiceStatus:
    """Check that Gamma, the CLOB, the Data API and Perps answer, and whether this network's
    location may place orders (Polymarket's geoblock check). Use it when other tools fail with
    network errors."""
    checks = [
        ("gamma", ep.GAMMA_STATUS),
        ("clob", ep.CLOB_TIME),
        ("data_api", ep.DATA_STATUS),
        ("perps", ep.PERPS_PING),
        ("perps_time", ep.PERPS_TIME),
    ]
    results = await asyncio.gather(*(_check(call, name, endpoint) for name, endpoint in checks))
    geo = await geoblock(call)
    note = None
    if geo.blocked:
        note = "Order placement is blocked from this location. Reading market data still works."
    return ServiceStatus(services=list(results), geoblock=geo, note=note)


class HiddenGroup(CompactModel):
    reason: str
    tools: list[str]


class AuthStatus(CompactModel):
    signer_address: str | None = None
    wallet_address: str | None = None
    signer_role: str
    signing_extra_installed: bool
    credentials: list[str]
    gates_open: list[str]
    toolsets: list[str]
    hidden: list[HiddenGroup]
    notes: list[str] = []


@operation("Show what this server can do", toolset="system", read_only=True)
async def auth_status(call: Call) -> AuthStatus:
    """Report the configured signer and wallet, which credentials were found, which write gates
    are open, and which tools are hidden and what would enable them. Secrets are never shown.
    Tools appear only after the server restarts with the new configuration."""
    runtime = call.runtime
    caps = runtime.caps
    identity = runtime.identity
    groups: dict[str, list[str]] = {}
    for op, reason in hidden_operations(caps):
        groups.setdefault(reason, []).append(op.name)
    return AuthStatus(
        signer_address=identity.signer_address,
        wallet_address=identity.wallet_address,
        signer_role=runtime.settings.signer_role,
        signing_extra_installed=signing_available(),
        credentials=sorted(caps.have),
        gates_open=sorted(caps.gates),
        toolsets=sorted(caps.toolsets),
        hidden=[HiddenGroup(reason=reason, tools=tools) for reason, tools in groups.items()],
        notes=list(caps.notes),
    )
