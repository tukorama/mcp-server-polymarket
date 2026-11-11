"""Combos, collateral return, bridge withdrawals, builder approvals and BLP enrollment."""

import pytest
from mcp.types import ElicitResult

pytest.importorskip("eth_account")

from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.client.session import (  # noqa: E402
    ClobCredentials,
    CredentialStore,
    PerpsSession,
)
from mcp_server_polymarket.signing import eip712, orders, perps  # noqa: E402
from mcp_server_polymarket.signing.wallets import (  # noqa: E402
    EXCHANGE_V3,
    ROUTER_V2,
    derive_deposit_wallet,
)
from tests.mcp.helpers import call, error, session  # noqa: E402

pytestmark = pytest.mark.anyio
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


async def accept(_context, _params):
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.fixture
def everything(settings, api):
    settings.toolsets = "all"
    settings.private_key = KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    settings.allow_trading = settings.allow_transfers = True
    settings.relayer_api_key, settings.relayer_api_key_address = "rk", OWNER
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    store.save_clob(ClobCredentials(OWNER, "k", "c2VjcmV0", "p"))
    store.save_perps(PerpsSession(owner=OWNER, proxy="0x" + "2" * 40, secret="s"))
    api.add(ep.WEB_GEOBLOCK, {"blocked": False})
    return settings


QUOTE = {
    "rfq_id": "r1",
    "status": "AWAITING_REQUESTER_ACCEPTANCE",
    "request": {"yes_position_id": "777"},
    "quote": {
        "quote_id": "q1",
        "blended_price_e6": "500000",
        "maker_amount_e6": "966191",
        "taker_amount_e6": "1932381",
    },
}


async def test_trade_combo_requests_checks_and_accepts(everything, api):
    api.add(ep.COMBO_REQUEST_CREATE, QUOTE)
    api.add(ep.COMBO_REQUEST_ACCEPT, {"rfq_id": "r1", "status": "EXECUTING"})
    async with session(everything, api, elicitation_callback=accept) as client:
        data = await call(
            client, "trade_combo", legs=["1", "2"], direction="BUY", amount="1", worst_price="0.55"
        )
    assert data["status"] == "accepted" and data["state"] == "EXECUTING"
    request = api.body(ep.COMBO_REQUEST_CREATE)
    assert request["requested_size"] == {"unit": "notional", "value_e6": "1000000"}
    assert request["signer_address"] == request["maker_address"] == everything.wallet_address
    signed = api.body(ep.COMBO_REQUEST_ACCEPT)["signed_order"]
    order = orders.Order(
        token_id="777",
        side="BUY",
        maker=signed["maker"],
        signer=signed["signer"],
        maker_amount=966191,
        taker_amount=1932381,
        signature_type=3,
        salt=int(signed["salt"]),
        timestamp_ms=int(signed["timestamp"]),
        exchange=EXCHANGE_V3,
        version="3",
    )
    assert eip712.recover(orders.order_typed_data(order), signed["signature"][:132]) == OWNER
    assert int(signed["timestamp"]) < 10**11, "Combo orders are timestamped in seconds"


async def test_trade_combo_declines_a_bad_price(everything, api):
    api.add(ep.COMBO_REQUEST_CREATE, QUOTE)
    async with session(everything, api, elicitation_callback=accept) as client:
        data = await call(
            client, "trade_combo", legs=["1", "2"], direction="BUY", amount="1", worst_price="0.4"
        )
    assert data["status"] == "declined" and api.calls(ep.COMBO_REQUEST_ACCEPT) == []


async def test_collateral_return_refuses_foreign_targets(everything, api):
    plan = {"plan_hash": "0xph", "router_call": {"to": "0x" + "6" * 40, "data": "0xdead"}}
    api.add(ep.COLLATERAL_RETURN_PLAN, plan)
    async with session(everything, api, elicitation_callback=accept) as client:
        assert "Router" in await error(client, "execute_collateral_return", plan_hash="0xph")
        assert "changed" in await error(client, "execute_collateral_return", plan_hash="0xother")
    api.add(
        ep.COLLATERAL_RETURN_PLAN,
        {"plan_hash": "0xph", "router_call": {"to": ROUTER_V2, "data": "0xab"}},
    )
    api.add(ep.RELAYER_TX_PARAMS, {"nonce": "2"})
    api.add(ep.COLLATERAL_RETURN_SUBMIT, {"transactionID": "t"})
    async with session(everything, api, elicitation_callback=accept) as client:
        data = await call(client, "execute_collateral_return", plan_hash="0xph")
    assert data["status"] == "submitted"
    body = api.body(ep.COLLATERAL_RETURN_SUBMIT)
    assert body["plan_hash"] == "0xph" and body["envelope"]["type"] == "WALLET"
    assert api.calls(ep.COLLATERAL_RETURN_SUBMIT)[0].headers["RELAYER_API_KEY"] == "rk"


async def test_bridge_withdrawal_addresses_respect_the_allowlist(everything, api):
    api.add(ep.BRIDGE_WITHDRAW, {"address": {"evm": "0xbridge"}, "note": "send pUSD here"})
    token = "0x" + "a" * 40
    async with session(everything, api, elicitation_callback=accept) as client:
        assert "allowlist" in await error(
            client,
            "create_withdrawal_addresses",
            to_chain_id="8453",
            to_token_address=token,
            recipient="0x" + "8" * 40,
        )
        data = await call(
            client, "create_withdrawal_addresses", to_chain_id="8453", to_token_address=token
        )
    assert data["addresses"] == {"evm": "0xbridge"}
    assert api.body(ep.BRIDGE_WITHDRAW)["recipientAddr"] == everything.wallet_address


async def test_builder_approval_bumps_the_version(everything, api):
    everything.perps_builder = "0x" + "b" * 40
    api.add(ep.PERPS_BUILDER_APPROVALS, [{"approval_version": 2}])
    api.add(ep.PERPS_APPROVE_BUILDER, {"builder": everything.perps_builder, "approval_version": 3})
    async with session(everything, api, elicitation_callback=accept) as client:
        await call(client, "set_perps_builder_approval", max_fee_rate="0.0005")
        assert "capped" in await error(client, "set_perps_builder_approval", max_fee_rate="0.01")
    body = api.body(ep.PERPS_APPROVE_BUILDER)
    assert body["op"]["args"]["approval_version"] == 3
    op = perps.approve_builder(everything.perps_builder, "0.0005", 3)
    typed = perps.op_typed_data(op, salt=body["salt"], ts_ms=body["ts"])
    assert eip712.recover(typed, body["sig"]) == OWNER


async def test_blp_enrollment(everything, api):
    api.add(ep.PERPS_BLP_ENROLL, {"status": "ok"})
    async with session(everything, api, elicitation_callback=accept) as client:
        await call(client, "set_perps_blp_enrollment", instrument_id=1, active=True)
    assert api.body(ep.PERPS_BLP_ENROLL) == {"instrument_id": 1, "active": True}
