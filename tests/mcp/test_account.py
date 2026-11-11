import base64
import hashlib
import hmac

import pytest

from mcp_server_polymarket.client import endpoints as ep
from tests.mcp.helpers import call, session

pytestmark = pytest.mark.anyio
SECRET = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


@pytest.fixture
def account(settings):
    settings.api_key, settings.api_secret, settings.api_passphrase = "key-1", SECRET, "pass-1"
    settings.address = ANVIL
    return settings


def check_hmac(request, path: str) -> None:
    """The server's view: recompute the L2 signature over the exact bytes received."""
    timestamp = request.headers["POLY_TIMESTAMP"]
    message = f"{timestamp}{request.method}{path}".encode() + (request.content or b"")
    expected = base64.urlsafe_b64encode(
        hmac.new(base64.urlsafe_b64decode(SECRET), message, hashlib.sha256).digest()
    ).decode()
    assert request.headers["POLY_SIGNATURE"] == expected
    assert request.headers["POLY_API_KEY"] == "key-1"
    assert request.headers["POLY_ADDRESS"] == ANVIL


async def test_account_tools_are_hidden_without_credentials(settings, api):
    async with session(settings, api) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "list_my_open_orders" not in names and "search" in names


async def test_status_survives_failed_sections(account, api):
    api.add(ep.CLOB_CLOSED_ONLY, {"closed_only": False})
    api.add(ep.CLOB_RATE_LIMIT_TIER, (500, {"error": "boom"}))
    async with session(account, api) as client:
        data = await call(client, "get_my_account_status")
    assert data["closed_only"] is False and data["errors"][0]["section"] == "rate_limit_tier"
    check_hmac(api.calls(ep.CLOB_CLOSED_ONLY)[0], "/auth/ban-status/closed-only")


async def test_status_with_signer_lists_api_keys_and_session_keys(account, api):
    account.private_key = ANVIL_KEY
    account.wallet_address = "0x94bf330955a0b957662feaf878de77bf25f76cd9"
    account.api_key = None  # credentials will be derived by `auth`; here only L1 is used
    account.address = None
    from mcp_server_polymarket.client.session import ClobCredentials, CredentialStore

    CredentialStore(account.resolved_session_file, account.resolved_perps_session_file).save_clob(
        ClobCredentials(ANVIL, "key-1", SECRET, "pass-1")
    )
    api.add(ep.CLOB_CLOSED_ONLY, {"closed_only": True})
    api.add(ep.CLOB_RATE_LIMIT_TIER, {"tier": "Standard"})
    api.add(ep.CLOB_API_KEYS, {"apiKeys": ["key-1"]})
    api.add(ep.CLOB_SESSION_SIGNERS, {"signers": []})
    # A Deposit Wallet derived from this key is not this key's wallet, so classify as such:
    from mcp_server_polymarket.signing.wallets import derive_deposit_wallet

    account.wallet_address = derive_deposit_wallet(ANVIL)
    async with session(account, api) as client:
        data = await call(client, "get_my_account_status")
    assert data["api_keys"] == ["key-1"] and data["signature_type"] == 3
    assert data["session_keys"] == [] and data["closed_only"] is True
    assert api.calls(ep.CLOB_API_KEYS)[0].headers["POLY_NONCE"] == "0"


async def test_orders_trades_and_notifications(account, api):
    api.add(ep.CLOB_ORDERS, {"data": [{"id": "0x1", "price": "0.5"}], "next_cursor": "LTE="})
    api.add(ep.CLOB_WALLET_OPEN_ORDERS, {"data": [{"id": "0x2"}], "next_cursor": "MTA="})
    api.add(ep.CLOB_ORDER, {"id": "0x1", "status": "MATCHED"})
    api.add(ep.CLOB_TRADES, {"data": [{"id": "t", "status": "CONFIRMED"}], "next_cursor": "LTE="})
    api.add(ep.CLOB_NOTIFICATIONS, [{"id": 1, "type": 1}])
    async with session(account, api) as client:
        data = await call(client, "list_my_open_orders", condition_id="0xc")
        assert data["items"] == [{"id": "0x1", "price": "0.5"}] and "next_cursor" not in data
        assert api.query(ep.CLOB_ORDERS) == {"market": "0xc"}
        data = await call(client, "list_my_open_orders", whole_wallet=True)
        assert data["next_cursor"] == "MTA="
        assert (await call(client, "get_my_order", order_id="0x1"))["status"] == "MATCHED"
        await call(client, "list_my_trades", after="2026-11-01")
        assert api.query(ep.CLOB_TRADES) == {"maker_address": ANVIL, "after": "1793491200"}
        assert (await call(client, "list_my_notifications"))["items"][0]["id"] == 1
    check_hmac(api.calls(ep.CLOB_ORDER)[0], "/data/order/0x1")


async def test_balance_allowance_picks_the_asset_type(account, api):
    account.signature_type = 1
    api.add(ep.CLOB_BALANCE_ALLOWANCE, {"balance": "2500000", "allowances": {"0xex": "max"}})
    api.add(ep.CLOB_BALANCE_ALLOWANCE_UPDATE, {})
    v2_token = str((1 << 248) + 1)
    async with session(account, api) as client:
        data = await call(client, "get_my_balance_allowance", refresh=True)
        assert data["balance_display"] == "2.5" and data["refreshed"] is True
        assert api.query(ep.CLOB_BALANCE_ALLOWANCE) == {
            "asset_type": "COLLATERAL",
            "signature_type": "1",
        }
        await call(client, "get_my_balance_allowance", token_id=v2_token)
        assert api.query(ep.CLOB_BALANCE_ALLOWANCE)["asset_type"] == "CONDITIONAL-V2"
        await call(client, "get_my_balance_allowance", token_id="123" * 20)
        assert api.query(ep.CLOB_BALANCE_ALLOWANCE)["asset_type"] == "CONDITIONAL"


async def test_wallet_status_and_relayer_transactions(account, api):
    account.relayer_api_key, account.relayer_api_key_address = "rk", ANVIL
    account.signature_type = 2
    api.add(ep.RELAYER_DEPLOYED, {"deployed": True})
    api.add(ep.RELAYER_NONCE, {"nonce": "4"})
    api.add(ep.RELAYER_RELAY_PAYLOAD, {"address": "0xrelay", "nonce": "4"})
    api.add(ep.RELAYER_TX, {"transactionID": "tx", "state": "STATE_CONFIRMED"})
    api.add(ep.RELAYER_TRANSACTION, [{"transactionID": "tx2", "state": "STATE_MINED"}])
    api.add(ep.RELAYER_TRANSACTIONS, [{"transactionID": "tx3"}])
    async with session(account, api) as client:
        data = await call(client, "get_my_wallet_status")
        assert data == {
            "signer": ANVIL,
            "kind": "GNOSIS_SAFE",
            "deployed": True,
            "nonce": "4",
            "relayer": {"address": "0xrelay", "nonce": "4"},
        }
        assert api.query(ep.RELAYER_NONCE) == {"address": ANVIL, "type": "SAFE"}
        assert (await call(client, "get_relayer_transaction", transaction_id="tx"))["state"]
        legacy = await call(client, "get_relayer_transaction", transaction_id="tx2", kind="legacy")
        assert legacy["state"] == "STATE_MINED"
        assert (await call(client, "list_relayer_transactions"))["items"]
    assert api.calls(ep.RELAYER_TRANSACTIONS)[0].headers["RELAYER_API_KEY"] == "rk"


@pytest.fixture
def perps(settings):
    settings.perps_proxy, settings.perps_secret = "0x" + "2" * 40, "perps-secret"
    return settings


async def test_perps_account_reads(perps, api):
    api.add(ep.PERPS_CREDENTIALS, {"address": "0xa", "keys": []})
    api.add(ep.PERPS_ACCOUNT_STATS, {"taker_volume": "1"})
    api.add(ep.PERPS_ACCOUNT_LIMITS, {"open_orders": 1})
    api.add(ep.PERPS_AUTO_CANCEL_STATUS, {"deadline": 0})
    api.add(ep.PERPS_REFERRAL, (404, {"status": "err", "error": "not_found"}))
    api.add(ep.PERPS_ACCOUNT_CONFIG, {"leverage": 5})
    api.add(ep.PERPS_PORTFOLIO, {"equity": "10", "positions": []})
    api.add(ep.PERPS_BALANCES, [{"asset": "pUSD", "balance": "10"}])
    async with session(perps, api) as client:
        data = await call(client, "get_my_perps_account", instrument_id=1)
        assert data["stats"] == {"taker_volume": "1"} and data["errors"][0]["section"] == "referral"
        assert data["instrument_config"] == {"leverage": 5}
        portfolio = await call(client, "get_my_perps_portfolio")
        assert portfolio["balances"][0]["asset"] == "pUSD"
    headers = api.calls(ep.PERPS_PORTFOLIO)[0].headers
    assert headers["POLYMARKET-PROXY"] == "0x" + "2" * 40
    assert headers["POLYMARKET-SECRET"] == "perps-secret"


async def test_perps_history_lists(perps, api):
    page = {"data": [{"timestamp": 1794435252089, "value": "1"}], "more": True, "cursor": "n"}
    for endpoint in (
        ep.PERPS_OPEN_ORDERS,
        ep.PERPS_ORDERS,
        ep.PERPS_FILLS,
        ep.PERPS_EQUITY,
        ep.PERPS_PNL,
        ep.PERPS_FUNDING,
        ep.PERPS_BACKSTOPS,
        ep.PERPS_DEPOSITS,
        ep.PERPS_WITHDRAWALS,
        ep.PERPS_INTERNAL_TRANSFERS,
    ):
        api.add(endpoint, page)
    api.add(ep.PERPS_ACCOUNT_REWARDS, {"rewards": []})
    api.add(ep.PERPS_NOTIFICATIONS, {"data": [], "has_more": False})
    async with session(perps, api) as client:
        data = await call(client, "list_my_perps_orders")
        assert data["items"][0]["timestamp"].endswith("Z") and data["has_more"] is True
        await call(client, "list_my_perps_orders", open_only=False, client_order_id="c" * 32)
        assert api.query(ep.PERPS_ORDERS) == {"client_order_id": "c" * 32}
        await call(client, "list_my_perps_fills", oldest_first=True, cursor="x")
        assert api.query(ep.PERPS_FILLS) == {"sort": "asc", "cursor": "x"}
        data = await call(client, "get_my_perps_history", interval="1d", count=10)
        query = api.query(ep.PERPS_EQUITY)
        assert query["interval"] == "1d" and "start_timestamp" in query
        await call(client, "get_my_perps_history", series="pnl", interval="1h")
        result = await client.call_tool("get_my_perps_history", {"series": "pnl", "interval": "5m"})
        assert result.is_error
        await call(client, "list_my_perps_funding", instrument_id=2)
        await call(client, "list_my_perps_funding", kind="backstops", cursor="c")
        await call(client, "list_my_perps_transfers", status="pending")
        assert api.query(ep.PERPS_DEPOSITS) == {"deposit_status": "pending"}
        await call(client, "list_my_perps_transfers", kind="withdrawals", status="failed")
        await call(client, "list_my_perps_transfers", kind="internal", direction="out")
        await call(client, "get_my_perps_rewards", date="2026-11-10")
        await call(client, "list_my_perps_notifications", since_seq=5)
        assert api.query(ep.PERPS_NOTIFICATIONS) == {"limit": "30", "since_seq": "5"}
