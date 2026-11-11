import json
from decimal import Decimal

import httpx2
import pytest
from mcp.types import ElicitResult

pytest.importorskip("eth_account")

from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.client.session import ClobCredentials, CredentialStore  # noqa: E402
from mcp_server_polymarket.signing import eip712, orders  # noqa: E402
from mcp_server_polymarket.signing.wallets import derive_deposit_wallet  # noqa: E402
from tests.conftest import fixture  # noqa: E402
from tests.mcp.helpers import call, error, session  # noqa: E402

pytestmark = pytest.mark.anyio
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TOKEN = "60133385087854467667298832945990887425905703052255634714562642069595733878370"
BOOK = {
    **fixture("clob_book"),
    "asset_id": TOKEN,
    "bids": [{"price": "0.40", "size": "100"}, {"price": "0.39", "size": "50"}],
    "asks": [{"price": "0.42", "size": "80"}, {"price": "0.45", "size": "500"}],
    "tick_size": "0.01",
    "min_order_size": "5",
    "neg_risk": False,
}
MARKET = {
    "markets": [
        {
            "question": "Will it rain?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{TOKEN}", "1"]',
        }
    ]
}
ASKED: list[str] = []


async def accept(_context, params):
    ASKED.append(params.message)
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.fixture
def trader(settings, api):
    ASKED.clear()
    settings.private_key = KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    settings.allow_trading = True
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    store.save_clob(ClobCredentials(OWNER, "owner-key", "c2VjcmV0", "pass"))
    api.add(ep.CLOB_BOOK, BOOK)
    api.add(ep.GAMMA_MARKETS_KEYSET, MARKET)
    api.add(ep.WEB_GEOBLOCK, {"blocked": False, "country": "HU"})
    return settings


def trade(settings, api):
    return session(settings, api, elicitation_callback=accept)


async def test_trading_tools_need_the_gate(settings, api):
    settings.private_key = KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    async with session(settings, api) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "place_limit_order" not in names and "cancel_orders" not in names


async def test_limit_order_is_confirmed_signed_and_sent(trader, api):
    api.add(ep.CLOB_POST_ORDER, {"success": True, "orderID": "0xabc", "status": "live"})
    async with trade(trader, api) as client:
        data = await call(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
    assert data["status"] == "placed" and data["order_id"] == "0xabc"
    assert data["preview"]["notional_usd"] == "4.10"
    assert [
        f"BUY 10 shares of 'Yes' in 'Will it rain?' at 0.41 pUSD each: pay up to $4.10, limit "
        f"order until cancelled. Wallet {trader.wallet_address}. Place this order?"
    ] == ASKED
    body = api.body(ep.CLOB_POST_ORDER)
    assert body["owner"] == "owner-key" and body["orderType"] == "GTC"
    wire = body["order"]
    assert wire["makerAmount"] == "4100000" and wire["takerAmount"] == "10000000"
    assert wire["maker"] == wire["signer"] == trader.wallet_address
    assert wire["signatureType"] == 3
    rebuilt = orders.Order(
        token_id=TOKEN,
        side="BUY",
        maker=wire["maker"],
        signer=wire["signer"],
        maker_amount=4_100_000,
        taker_amount=10_000_000,
        signature_type=3,
        salt=wire["salt"],
        timestamp_ms=int(wire["timestamp"]),
        exchange=orders.exchange_for(TOKEN, neg_risk=False)[0],
        version="2",
    )
    typed = orders.order_typed_data(rebuilt)
    assert eip712.recover(typed, wire["signature"][: 2 + 130]) == OWNER
    assert wire["signature"].endswith("00ba")


async def test_dry_run_sends_nothing_and_asks_nothing(trader, api):
    async with session(trader, api) as client:
        data = await call(
            client,
            "place_limit_order",
            token_id=TOKEN,
            side="SELL",
            price="0.45",
            size="10",
            dry_run=True,
        )
    assert data["status"] == "preview" and data["preview"]["maker_amount"] == 10_000_000
    assert api.calls(ep.CLOB_POST_ORDER) == [] and ASKED == []


async def test_client_without_elicitation_cannot_trade(trader, api):
    async with session(trader, api) as client:
        text = await error(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
    assert "cannot ask the user" in text and api.calls(ep.CLOB_POST_ORDER) == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"price": "0.415", "size": "10"}, "not on this market's tick size"),
        ({"price": "0.41", "size": "2"}, "below this market's minimum"),
        ({"price": "0.60", "size": "10"}, "above the best price"),
        ({"price": "0.41", "size": "1000"}, "above the limit"),
    ],
)
async def test_checks_refuse_before_signing(trader, api, arguments, message):
    async with trade(trader, api) as client:
        text = await error(client, "place_limit_order", token_id=TOKEN, side="BUY", **arguments)
    assert message in text
    assert api.calls(ep.CLOB_POST_ORDER) == []


async def test_zero_limit_blocks_and_unlimited_lifts(trader, api, monkeypatch):
    trader.max_order_usd = "0"
    async with trade(trader, api) as client:
        assert "switched off" in await error(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )


async def test_geoblocked_location_fails_closed(trader, api):
    api.add(ep.WEB_GEOBLOCK, {"blocked": True, "country": "US", "region": "NY"})
    async with trade(trader, api) as client:
        text = await error(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
    assert "US-NY" in text and api.calls(ep.CLOB_POST_ORDER) == []


async def test_rejection_is_a_result_not_an_error(trader, api):
    api.add(ep.CLOB_POST_ORDER, {"success": False, "errorMsg": "not enough balance / allowance"})
    async with trade(trader, api) as client:
        data = await call(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
    assert data["status"] == "rejected" and "allowance" in data["message"]


async def test_unknown_outcome_is_journaled_blocked_and_resendable(trader, api):
    def drop(request):
        raise httpx2.ReadError("connection reset")

    api.add(ep.CLOB_POST_ORDER, (200, drop))
    async with trade(trader, api) as client:
        data = await call(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
        assert data["status"] == "unknown" and data["request_id"]
        assert len(api.calls(ep.CLOB_POST_ORDER)) == 1, "a write is never retried"
        blocked = await error(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
        assert data["request_id"] in blocked
        pending = await call(client, "list_unconfirmed_writes")
        assert pending["writes"][0]["resendable"] is True
        api.add(ep.CLOB_POST_ORDER, {"success": True, "orderID": "0xabc"})
        resent = await call(client, "resend_write", request_id=data["request_id"])
        assert resent["status"] == "sent"
    first, second = api.calls(ep.CLOB_POST_ORDER)[0], api.calls(ep.CLOB_POST_ORDER)[-1]
    assert first.content == second.content, "the identical signed body"


async def test_market_order_bound_and_amounts(trader, api):
    api.add(ep.CLOB_POST_ORDER, {"success": True, "orderID": "0x1", "status": "matched"})
    async with trade(trader, api) as client:
        data = await call(
            client,
            "place_market_order",
            token_id=TOKEN,
            side="BUY",
            amount="10",
            price_bound="0.45",
            order_type="FOK",
        )
    assert data["order_status"] == "matched"
    wire = api.body(ep.CLOB_POST_ORDER)
    maker, taker = int(wire["order"]["makerAmount"]), int(wire["order"]["takerAmount"])
    assert maker == 10_000_000 and Decimal(maker) / Decimal(taker) >= Decimal("0.45")
    assert wire["orderType"] == "FOK"
    assert "Spend up to $10" in ASKED[0]


async def test_batch_orders(trader, api):
    api.add(
        ep.CLOB_POST_ORDERS,
        [{"success": True, "orderID": "0x1"}, {"success": True, "errorMsg": "Duplicated."}],
    )
    specs = [
        {"token_id": TOKEN, "side": "BUY", "price": "0.40", "size": "10"},
        {"token_id": TOKEN, "side": "SELL", "price": "0.43", "size": "10", "post_only": True},
    ]
    async with trade(trader, api) as client:
        data = await call(client, "place_orders", orders=specs)
    assert data["status"] == "partial" and data["orders"][1]["status"] == "rejected"
    body = api.body(ep.CLOB_POST_ORDERS)
    assert len(body) == 2 and body[1]["postOnly"] is True
    assert "Place 2 limit orders" in ASKED[0]


async def test_cancels_need_only_the_cancel_gate(settings, api):
    settings.allow_cancel = True
    settings.api_key, settings.api_secret, settings.api_passphrase = "k", "c2VjcmV0", "p"
    settings.address = OWNER
    api.add(ep.CLOB_CANCEL_ORDER, {"canceled": ["0x1"], "not_canceled": {}})
    api.add(ep.CLOB_CANCEL_ORDERS, {"canceled": ["0x1"], "not_canceled": {"0x2": "not found"}})
    api.add(ep.CLOB_CANCEL_ALL, {"canceled": [], "not_canceled": {}})
    api.add(ep.CLOB_CANCEL_MARKET, {"canceled": ["0x3"]})
    api.add(ep.CLOB_CANCEL_WALLET, {"canceled": ["0x4"]})
    api.add(ep.CLOB_DROP_NOTIFICATIONS, None)
    async with session(settings, api) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "cancel_orders" in names and "place_limit_order" not in names
        assert (await call(client, "cancel_orders", order_ids=["0x1"]))["status"] == "canceled"
        assert api.body(ep.CLOB_CANCEL_ORDER) == {"orderID": "0x1"}
        data = await call(client, "cancel_orders", order_ids=["0x1", "0x2"])
        assert data["status"] == "partial" and data["not_canceled"] == {"0x2": "not found"}
        assert (await call(client, "cancel_all_orders"))["status"] == "none_canceled"
        await call(client, "cancel_all_orders", condition_id="0xc")
        assert api.body(ep.CLOB_CANCEL_MARKET) == {"market": "0xc"}
        await call(client, "cancel_all_orders", wallet_wide=True)
        assert api.body(ep.CLOB_CANCEL_WALLET) == {"all": True}
        await call(client, "mark_notifications_read", notification_ids=["1", "2"])
        assert api.query(ep.CLOB_DROP_NOTIFICATIONS) == {"ids": "1,2"}


async def test_v2_mismatch_is_refused(trader, api):
    api.add(ep.CLOB_BOOK, {**BOOK, "version": "v2"})
    async with trade(trader, api) as client:
        text = await error(
            client, "place_limit_order", token_id=TOKEN, side="BUY", price="0.41", size="10"
        )
    assert "wrong exchange" in text


def test_body_bytes_are_compact_json():
    from mcp_server_polymarket.client.base import encode_body

    assert (
        encode_body({"a": [1, "é"]})
        == json.dumps({"a": [1, "é"]}, separators=(",", ":"), ensure_ascii=False).encode()
    )
