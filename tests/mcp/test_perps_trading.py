import pytest
from mcp.types import ElicitResult

pytest.importorskip("eth_account")

from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.client.session import CredentialStore, PerpsSession  # noqa: E402
from mcp_server_polymarket.signing import eip712, perps  # noqa: E402
from mcp_server_polymarket.signing.wallets import PUSD, derive_deposit_wallet  # noqa: E402
from tests.conftest import fixture  # noqa: E402
from tests.mcp.helpers import call, error, session  # noqa: E402

pytestmark = pytest.mark.anyio
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
PROXY_KEY = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PROXY = eip712.address_of(PROXY_KEY) if True else None
INSTRUMENT = fixture("perps_instruments")[0]  # SP500-USD, price 1 decimal, quantity 5
ASKED: list[str] = []


async def accept(_context, params):
    ASKED.append(params.message)
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.fixture
def trader(settings, api):
    ASKED.clear()
    settings.private_key = OWNER_KEY
    settings.wallet_address = derive_deposit_wallet(OWNER)
    settings.allow_trading = True
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    store.save_perps(PerpsSession(owner=OWNER, proxy=PROXY, secret="s", proxy_key=PROXY_KEY))
    api.add(ep.PERPS_INSTRUMENTS, [INSTRUMENT])
    api.add(ep.PERPS_TICKERS, [{"instrument_id": 1, "mark_price": "7600"}])
    api.add(ep.WEB_GEOBLOCK, {"blocked": False})
    return settings


def recover(body) -> str:
    """Rebuild the signed op from the JSON body, as the server does, and recover the signer."""
    op_body = body["op"]
    kind, args = op_body["type"], op_body["args"]
    if kind == "createOrders":
        rows = []
        for a in args:
            trigger = None
            if "tr" in a:
                trigger = [a["tr"].get("market"), a["tr"]["trp"], a["tr"]["tpsl"]]
            rows.append(
                [
                    a["iid"],
                    a["buy"],
                    a.get("p"),
                    a["qty"],
                    a.get("tif"),
                    a["po"],
                    a.get("ro"),
                    a.get("c"),
                    trigger,
                ]
            )
        op = perps.create_orders(rows, op_body.get("grp"))
    elif kind == "updateLeverage":
        op = perps.update_leverage(args["iid"], args["lev"], args["cross"])
    elif kind == "cancelAll":
        op = perps.cancel_all(args.get("iid"))
    elif kind == "updateMargin":
        op = perps.update_margin(args["iid"], args["amt"])
    elif kind == "autoCancel":
        op = perps.auto_cancel(args["time"])
    elif kind == "internalTransfer":
        op = perps.internal_transfer(args["account"], args["token"], args["amount"], args["to"])
    else:
        op = [kind, args]
    typed = perps.op_typed_data(op, salt=body["salt"], ts_ms=body["ts"])
    return eip712.recover(typed, body["sig"])


async def test_bracket_order_is_signed_by_the_trading_key(trader, api):
    api.add(ep.PERPS_CREATE_ORDERS, [{"status": "ok", "oid": 1}, {"status": "ok", "oid": 2}])
    order = {
        "instrument_id": 1,
        "side": "BUY",
        "quantity": "0.01",
        "price": "7590",
        "take_profit": {"trigger_price": "7800"},
        "stop_loss": {"trigger_price": "7400", "limit_price": "7390"},
    }
    async with session(trader, api, elicitation_callback=accept) as client:
        data = await call(client, "place_perps_orders", orders=[order])
    assert data["status"] == "done"
    body = api.body(ep.PERPS_CREATE_ORDERS)
    assert body["op"]["grp"] == "order" and len(body["op"]["args"]) == 3
    exit_tp = body["op"]["args"][1]
    assert exit_tp == {
        "iid": 1,
        "buy": False,
        "po": False,
        "qty": "0.01",
        "ro": True,
        "tr": {"tpsl": "tp", "trp": "7800", "market": True},
    }
    assert recover(body) == PROXY
    assert "take profit at 7800" in ASKED[0]


async def test_order_checks(trader, api):
    async with session(trader, api, elicitation_callback=accept) as client:
        for order, message in [
            ({"price": "7590.25"}, "more than 1 decimals"),
            ({"price": "9000"}, "above the best price"),
            ({"quantity": "0.000001"}, "more than 5 decimals"),
            ({"quantity": "1"}, "above the limit"),
            ({"price": None}, "needs a price"),
        ]:
            spec = {"instrument_id": 1, "side": "BUY", "quantity": "0.01", "price": "7590"}
            spec.update(order)
            if spec["price"] is None:
                del spec["price"]
            text = await error(client, "place_perps_orders", orders=[spec])
            assert message in text, (order, text)
    assert api.calls(ep.PERPS_CREATE_ORDERS) == []


async def test_reduce_only_is_exempt_from_the_usd_limit(trader, api):
    api.add(ep.PERPS_CREATE_ORDERS, [{"status": "ok", "oid": 9}])
    spec = {
        "instrument_id": 1,
        "side": "SELL",
        "quantity": "1",
        "price": "7590",
        "reduce_only": True,
    }
    async with session(trader, api, elicitation_callback=accept) as client:
        data = await call(client, "place_perps_orders", orders=[spec])
    assert data["status"] == "done"


async def test_leverage_cap_and_signature(trader, api):
    api.add(ep.PERPS_LEVERAGE, {"status": "ok"})
    api.add(
        ep.PERPS_LEVERAGE_BATCH, [{"status": "ok"}, {"status": "err", "error": "invalid_leverage"}]
    )
    async with session(trader, api, elicitation_callback=accept) as client:
        assert "POLYMARKET_MAX_PERPS_LEVERAGE" in await error(
            client, "set_perps_leverage", changes=[{"instrument_id": 1, "leverage": 10}]
        )
        await call(client, "set_perps_leverage", changes=[{"instrument_id": 1, "leverage": 3}])
        assert recover(api.body(ep.PERPS_LEVERAGE)) == PROXY
        data = await call(
            client,
            "set_perps_leverage",
            changes=[
                {"instrument_id": 1, "leverage": 2},
                {"instrument_id": 1, "leverage": 3, "cross_margin": False},
            ],
        )
        assert data["status"] == "partial"


async def test_cancels_margin_auto_cancel(trader, api, clock):
    for endpoint in (ep.PERPS_CANCEL_ORDERS, ep.PERPS_CANCEL_ORDERS_COID, ep.PERPS_CANCEL_ALL):
        api.add(endpoint, [{"status": "ok"}])
    api.add(ep.PERPS_MARGIN, {"status": "ok"})
    api.add(ep.PERPS_AUTO_CANCEL, {"status": "ok", "deadline": 1})
    api.add(ep.PERPS_MODIFY_ORDERS, [{"status": "ok"}])
    api.add(ep.PERPS_MODIFY_ORDERS_COID, [{"status": "ok"}])
    async with session(trader, api, elicitation_callback=accept) as client:
        await call(client, "cancel_perps_orders", order_ids=[5, 6])
        assert api.body(ep.PERPS_CANCEL_ORDERS)["op"] == {"type": "cancelOrders", "args": [5, 6]}
        await call(client, "cancel_perps_orders", client_order_ids=["a" * 32])
        await call(client, "cancel_all_perps_orders", instrument_id=1)
        assert recover(api.body(ep.PERPS_CANCEL_ALL)) == PROXY
        await call(client, "adjust_perps_margin", instrument_id=1, amount="-5")
        assert api.body(ep.PERPS_MARGIN)["op"]["args"] == {"amt": "-5", "iid": 1}
        await call(client, "set_perps_auto_cancel")
        assert api.body(ep.PERPS_AUTO_CANCEL)["op"]["args"] == {"time": 0}
        await call(
            client, "modify_perps_orders", changes=[{"order_id": 5, "price": "1", "quantity": "2"}]
        )
        assert api.body(ep.PERPS_MODIFY_ORDERS)["op"]["args"] == [{"oid": 5, "p": "1", "qty": "2"}]
        await call(
            client,
            "modify_perps_orders",
            changes=[{"client_order_id": "b" * 32, "price": "1", "quantity": "2"}],
        )


async def test_housekeeping(trader, api):
    api.add(ep.PERPS_NOTIFICATIONS_READ, {"status": "ok"})
    api.add(ep.PERPS_CREATE_INVITE, {"status": "ok", "code": "ABC"})
    api.add(ep.PERPS_APPLY_REFERRAL, {"status": "ok"})
    async with session(trader, api, elicitation_callback=accept) as client:
        await call(client, "mark_perps_notifications_read", up_to_cursor="c")
        assert api.body(ep.PERPS_NOTIFICATIONS_READ) == {"before": "c"}
        assert (await call(client, "create_perps_invite_code"))["raw"]["code"] == "ABC"
        await call(client, "apply_perps_referral_code", code="XYZ")
        assert "only be done once" in ASKED[-1]


@pytest.fixture
def mover(trader):
    trader.allow_transfers = True
    trader.relayer_api_key, trader.relayer_api_key_address = "rk", OWNER
    return trader


async def test_withdraw_goes_to_own_wallet_and_is_owner_signed(mover, api):
    api.add(ep.PERPS_WITHDRAW, {"status": "ok", "withdraw_id": 3})
    async with session(mover, api, elicitation_callback=accept) as client:
        assert "allowlist" in await error(
            client, "withdraw_from_perps", amount="5", to="0x" + "9" * 40
        )
        await call(client, "withdraw_from_perps", amount="5")
    body = api.body(ep.PERPS_WITHDRAW)
    args = body["op"]["args"]
    assert args == {
        "account": OWNER,
        "token": PUSD,
        "amount": "5000000",
        "to": mover.wallet_address,
    }
    typed = perps.withdraw_typed_data(OWNER, PUSD, 5_000_000, args["to"], body["salt"], body["ts"])
    assert eip712.recover(typed, body["sig"]) == OWNER
    assert body["ts"] < 10**11, "seconds, not milliseconds"


async def test_transfers_need_their_own_gate(trader, api):
    async with session(trader, api) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "withdraw_from_perps" not in names and "place_perps_orders" in names


async def test_internal_transfer_needs_an_allowlisted_recipient(mover, api):
    target = "0x" + "7" * 40
    api.add(ep.PERPS_INTERNAL_TRANSFER, {"status": "ok", "transfer_id": 1})
    async with session(mover, api, elicitation_callback=accept) as client:
        assert "allowlist" in await error(client, "transfer_perps_internal", amount="2", to=target)
        mover.transfer_allowlist = target
    async with session(mover, api, elicitation_callback=accept) as client:
        await call(client, "transfer_perps_internal", amount="2", to=target)
    assert recover(api.body(ep.PERPS_INTERNAL_TRANSFER)) == OWNER


async def test_deposit_is_a_relayer_batch(mover, api):
    api.add(ep.RELAYER_TX_PARAMS, {"nonce": "1"})
    api.add(ep.RELAYER_SUBMIT, {"transactionID": "t", "state": "STATE_NEW"})
    api.add(ep.RELAYER_TX, {"transactionID": "t", "state": "STATE_CONFIRMED"})
    async with session(mover, api, elicitation_callback=accept) as client:
        data = await call(client, "deposit_to_perps", amount="12")
        assert data["status"] == "confirmed"
        result = await client.call_tool("deposit_to_perps", {"amount": "5"})
        assert result.is_error, "below the 10 pUSD minimum"
    calls = api.body(ep.RELAYER_SUBMIT)["depositWalletParams"]["calls"]
    assert calls[1]["data"].startswith("0x") and len(calls) == 2
