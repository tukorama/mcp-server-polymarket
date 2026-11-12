"""Regression tests for the whole-project review."""

# The `user` fixture is imported from test_write_safety; using it as an argument is not a
# redefinition.
# ruff: noqa: F811

import asyncio
import threading
from decimal import Decimal

import pytest
from mcp.types import ElicitResult

pytest.importorskip("eth_account")

from mcp_server_polymarket import journal as journal_module  # noqa: E402
from mcp_server_polymarket import safety, wallet_tx  # noqa: E402
from mcp_server_polymarket.client import endpoints as ep  # noqa: E402
from mcp_server_polymarket.config import Settings  # noqa: E402
from mcp_server_polymarket.journal import Journal  # noqa: E402
from mcp_server_polymarket.ops.wallet import MarketContext, merge_plan  # noqa: E402
from mcp_server_polymarket.redaction import scrub  # noqa: E402
from mcp_server_polymarket.signing import relayer  # noqa: E402
from mcp_server_polymarket.signing import wallets as w  # noqa: E402
from tests.mcp.helpers import call, error, session  # noqa: E402
from tests.mcp.test_write_safety import ORDER, accept, asking, drop, user  # noqa: E402, F401

pytestmark = pytest.mark.anyio


def test_neg_risk_merges_approve_conditional_tokens():
    market = MarketContext("0x" + "ab" * 32, "Q", v2=False, neg_risk=True, tokens=["1", "2"])
    approve, _, revoke = merge_plan(market, 1_000_000).calls
    adapter = w.NEG_RISK_COLLATERAL_ADAPTER
    assert approve == relayer.erc1155_set_approval_for_all(w.CONDITIONAL_TOKENS, adapter)
    assert revoke == relayer.erc1155_set_approval_for_all(w.CONDITIONAL_TOKENS, adapter, False)


async def test_builder_approval_reads_the_data_wrapper_and_reports_refusals(user, api):
    user.perps_builder = "0x" + "b" * 40
    api.add(ep.PERPS_BUILDER_APPROVALS, {"data": [{"approval_version": 4}]})
    api.add(ep.PERPS_APPROVE_BUILDER, {"status": "err", "error": "stale approval_version"})
    async with asking(user, api) as client:
        data = await call(client, "set_perps_builder_approval", max_fee_rate="0.0005")
        assert data["status"] == "rejected" and "stale" in data["message"]
        assert (await call(client, "get_my_perps_builder", view="approvals"))["approvals"]
    assert api.body(ep.PERPS_APPROVE_BUILDER)["op"]["args"]["approval_version"] == 5


def test_a_dotenv_file_cannot_set_keys_or_wallets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "POLYMARKET_PRIVATE_KEY=0x" + "1" * 64 + "\n"
        "POLYMARKET_WALLET_ADDRESS=0x" + "d" * 40 + "\n"
        "POLYMARKET_TOOLSETS=all\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.private_key is None and settings.wallet_address is None
    assert settings.toolsets == "all"


@pytest.mark.parametrize("value", ["off", "none", "nan", "-1"])
def test_limits_reject_ambiguous_values(monkeypatch, value):
    monkeypatch.setenv("POLYMARKET_MAX_TRANSFER_USD", value)
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_fingerprints_ignore_spelling():
    assert safety.fingerprint("w", Decimal("10"), "0xAbC") == safety.fingerprint(
        "w", Decimal("10.00"), "0xabc"
    )
    assert safety.fingerprint("w", "0.5") == safety.fingerprint("w", Decimal("0.50"))
    assert safety.fingerprint("w", "10") != safety.fingerprint("w", "11")


async def test_a_retry_spelled_differently_is_still_blocked(user, api):
    api.add(ep.CLOB_POST_ORDER, (200, drop))
    async with asking(user, api) as client:
        first = await call(client, "place_limit_order", **ORDER)
        again = {**ORDER, "price": "0.410", "size": "10.0"}
        assert first["request_id"] in await error(client, "place_limit_order", **again)


def test_in_flight_and_abandoned_writes_block_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    entry = journal.record(
        tool="trading:x", endpoint="E", summary="s", usd=None, fingerprint="f", body=b"{}"
    )
    assert journal.pending_duplicate("f").id == entry, "still in flight"
    journal.close()
    later = journal_module._now() + journal_module.STALE_PENDING_SECONDS + 1
    monkeypatch.setattr(journal_module, "_now", lambda: later)
    reopened = Journal(path)
    assert [e.id for e in reopened.unknown()] == [entry], "the process died mid-send"


async def test_declining_a_bridge_withdrawal_is_a_result(user, api):
    async def decline(_context, _params):
        return ElicitResult(action="decline")

    async with session(user, api, elicitation_callback=decline) as client:
        data = await call(
            client, "create_withdrawal_addresses", to_chain_id="137", to_token_address=w.PUSD
        )
    assert data["status"] == "declined" and api.calls(ep.BRIDGE_WITHDRAW) == []


def test_bearer_tokens_and_quoted_values_are_masked():
    assert scrub("Authorization: Bearer abcdef123456") == "Authorization: Bearer ***"
    assert "abc def" not in scrub("{'secret': 'abc def'}")


async def test_perps_significant_figures_apply_to_fractional_prices_only(user, api):
    api.add(ep.PERPS_CREATE_ORDERS, [{"status": "ok"}])
    spec = {"instrument_id": 1, "side": "BUY", "quantity": "0.01234", "price": "7590"}
    async with asking(user, api) as client:
        # Quantity 0.01234 and a whole-number price are valid whatever their digits.
        assert (await call(client, "place_perps_orders", orders=[spec]))["status"] == "done"


async def test_a_redirect_is_never_a_placed_order(user, api):
    api.add(ep.CLOB_POST_ORDER, (307, {"location": "elsewhere"}))
    async with asking(user, api) as client:
        assert (await call(client, "place_limit_order", **ORDER))["status"] == "unknown"


def test_wallet_locks_belong_to_one_event_loop():
    locks = []

    def grab():
        async def inner():
            locks.append(wallet_tx.wallet_lock("0xABC"))

        asyncio.run(inner())

    for _ in range(2):
        thread = threading.Thread(target=grab)
        thread.start()
        thread.join()
    assert locks[0] is not locks[1]
