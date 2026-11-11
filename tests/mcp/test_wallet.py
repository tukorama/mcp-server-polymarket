import json

import pytest
from mcp.types import ElicitResult

pytest.importorskip("eth_account")

from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.client.session import ClobCredentials, CredentialStore  # noqa: E402
from mcp_server_polymarket.signing import eip712, relayer  # noqa: E402
from mcp_server_polymarket.signing import wallets as w  # noqa: E402
from tests.mcp.helpers import call, error, session  # noqa: E402

pytestmark = pytest.mark.anyio
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OWNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
CONDITION = "0x" + "ab" * 32
V2_CONDITION = "0x" + "cd" * 31 + "00"
ASKED: list[str] = []


async def accept(_context, params):
    ASKED.append(params.message)
    return ElicitResult(action="accept", content={"confirm": True})


def market(version="v1", neg_risk=False):
    return {
        "markets": [
            {
                "question": "Will it rain?",
                "conditionId": CONDITION,
                "version": version,
                "negRisk": neg_risk,
                "clobTokenIds": '["1", "2"]',
                "positionIds": '["11", "12"]',
            }
        ]
    }


@pytest.fixture
def owner(settings, api):
    ASKED.clear()
    settings.private_key = KEY
    settings.wallet_address = w.derive_deposit_wallet(OWNER)
    settings.relayer_api_key, settings.relayer_api_key_address = "rk", OWNER
    settings.allow_trading = True
    store = CredentialStore(settings.resolved_session_file, settings.resolved_perps_session_file)
    store.save_clob(ClobCredentials(OWNER, "k", "c2VjcmV0", "p"))
    api.add(ep.RELAYER_TX_PARAMS, {"address": OWNER, "nonce": "5"})
    api.add(ep.RELAYER_SUBMIT, {"transactionID": "tx-1", "state": "STATE_NEW"})
    api.add(
        ep.RELAYER_TX,
        {"transactionID": "tx-1", "state": "STATE_MINED"},
        {"transactionID": "tx-1", "state": "STATE_CONFIRMED", "transactionHash": "0xhash"},
    )
    return settings


def calls_of(api) -> list[relayer.Call]:
    params = api.body(ep.RELAYER_SUBMIT)["depositWalletParams"]
    return [relayer.Call(c["target"], c["data"], int(c["value"])) for c in params["calls"]]


def assert_owner_signed(api, wallet: str) -> None:
    body = api.body(ep.RELAYER_SUBMIT)
    params = body["depositWalletParams"]
    typed = relayer.batch_typed_data(
        wallet, calls_of(api), int(body["nonce"]), int(params["deadline"])
    )
    assert eip712.recover(typed, body["signature"]) == OWNER
    assert body["type"] == "WALLET" and body["from"] == OWNER and params["depositWallet"] == wallet


async def test_approvals_send_only_what_is_missing(owner, api):
    existing = {
        "contracts": [
            {"token": w.PUSD, "spender": w.EXCHANGE_V3, "approved": True},
            {"token": w.CONDITIONAL_TOKENS, "spender": w.STANDARD_EXCHANGE, "approved": False},
        ]
    }
    api.add(ep.DATA_APPROVALS, {"data": existing})
    async with session(owner, api, elicitation_callback=accept) as client:
        data = await call(client, "setup_trading_approvals")
    assert data["status"] == "confirmed" and data["transaction_hash"] == "0xhash"
    assert len(data["calls"]) == 7
    assert "neg-risk adapter" in ASKED[0]
    assert_owner_signed(api, owner.wallet_address)
    assert api.calls(ep.RELAYER_SUBMIT)[0].headers["RELAYER_API_KEY"] == "rk"


async def test_split_on_a_neg_risk_market_uses_its_adapter(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market(neg_risk=True))
    async with session(owner, api, elicitation_callback=accept) as client:
        preview = await call(
            client, "split_position", condition_id=CONDITION, amount="2.5", dry_run=True
        )
        assert preview["status"] == "preview" and api.calls(ep.RELAYER_SUBMIT) == []
        await call(client, "split_position", condition_id=CONDITION, amount="2.5")
    approve, split = calls_of(api)
    assert approve == relayer.erc20_approve(w.PUSD, w.NEG_RISK_COLLATERAL_ADAPTER, 2_500_000)
    assert split == relayer.ctf_split(w.NEG_RISK_COLLATERAL_ADAPTER, CONDITION, 2_500_000)
    assert "Split 2.5 pUSD" in ASKED[0]


async def test_merge_all_pairs_on_a_v2_market(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market(version="v2"))
    api.add(
        ep.DATA_POSITIONS,
        {
            "data": [
                {"outcome_index": 0, "current_size": 7.25},
                {"outcome_index": 1, "current_size": 3.5},
            ]
        },
    )
    async with session(owner, api, elicitation_callback=accept) as client:
        await call(client, "merge_positions", condition_id=V2_CONDITION)
    approval, merge = calls_of(api)
    assert approval == relayer.erc1155_set_approval_for_all(w.POSITION_MANAGER, w.ROUTER_V2)
    assert merge == relayer.router_merge(w.ROUTER_V2, V2_CONDITION, 3_500_000)
    assert api.query(ep.DATA_POSITIONS)["status"] == "MERGEABLE"


async def test_redeem(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market())
    async with session(owner, api, elicitation_callback=accept) as client:
        data = await call(client, "redeem_positions", condition_id=CONDITION)
    assert data["status"] == "confirmed"
    assert calls_of(api)[1] == relayer.ctf_redeem(w.COLLATERAL_ADAPTER, CONDITION)


async def test_v2_redeem_needs_something_to_redeem(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market(version="v2"))
    api.add(ep.DATA_POSITIONS, {"data": []})
    async with session(owner, api, elicitation_callback=accept) as client:
        assert "Nothing" in await error(client, "redeem_positions", condition_id=V2_CONDITION)
    assert api.calls(ep.RELAYER_SUBMIT) == []


async def test_nonce_mismatch_is_re_signed_once(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market())
    api.add(
        ep.RELAYER_SUBMIT,
        (400, {"error": "batch nonce 5 does not match on-chain nonce 6"}),
        {"transactionID": "tx-1", "state": "STATE_NEW"},
    )
    async with session(owner, api, elicitation_callback=accept) as client:
        await call(client, "split_position", condition_id=CONDITION, amount="1")
    first, second = (json.loads(r.content) for r in api.calls(ep.RELAYER_SUBMIT))
    assert first["nonce"] == "5" and second["nonce"] == "6"


async def test_slow_transactions_return_pending(owner, api):
    api.add(ep.GAMMA_MARKETS_KEYSET, market())
    api.add(ep.RELAYER_TX, {"transactionID": "tx-1", "state": "STATE_MINED"})
    async with session(owner, api, elicitation_callback=accept) as client:
        data = await call(client, "redeem_positions", condition_id=CONDITION)
    assert data["status"] == "pending" and "get_relayer_transaction" in data["next_step"]


async def test_wallet_tools_need_a_deposit_wallet(owner, api):
    owner.wallet_address = OWNER  # an EOA
    api.add(ep.GAMMA_MARKETS_KEYSET, market())
    async with session(owner, api, elicitation_callback=accept) as client:
        assert "Deposit Wallets only" in await error(
            client, "split_position", condition_id=CONDITION, amount="1"
        )
