import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.conftest import fixture
from tests.mcp.helpers import call, session

pytestmark = pytest.mark.anyio


async def test_service_status_reports_each_service(settings, api):
    api.add(ep.GAMMA_STATUS, "OK")
    api.add(ep.CLOB_TIME, 1794434027)
    api.add(ep.DATA_STATUS, fixture("data_status"))
    api.add(ep.PERPS_PING, {"status": "ok"})
    api.add(ep.PERPS_TIME, {"time": 1794430398315})
    api.add(ep.WEB_GEOBLOCK, fixture("web_geoblock"))
    async with session(settings, api) as client:
        data = await call(client, "get_service_status")
    assert all(service["ok"] for service in data["services"])
    assert data["geoblock"] == {"blocked": True, "country": "US", "region": "VA"}
    assert "blocked" in data["note"]


async def test_unreachable_service_is_reported_not_raised(settings, api, clock):
    api.add(ep.GAMMA_STATUS, (503, {"error": "down"}))
    async with session(settings, api) as client:
        data = await call(client, "get_service_status")
    gamma = next(s for s in data["services"] if s["service"] == "gamma")
    assert gamma["ok"] is False and "503" in gamma["error"]


async def test_auth_status_without_credentials(settings, api):
    async with session(settings, api) as client:
        data = await call(client, "auth_status")
    assert data["credentials"] == [] and data["gates_open"] == []
    reasons = " ".join(group["reason"] for group in data["hidden"])
    assert "auth" in reasons and "POLYMARKET_ALLOW_TRADING" in reasons
