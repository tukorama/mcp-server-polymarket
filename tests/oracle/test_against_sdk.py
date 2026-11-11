"""Differential tests against the official SDK's internals (CI job `oracle`).

    uv run --locked --all-extras --with polymarket-client==0.18.0 pytest tests/oracle -m oracle

The SDK is only a test oracle here: the package never imports it.
"""

import random
from decimal import Decimal

import pytest

pytest.importorskip("polymarket")
pytestmark = pytest.mark.oracle

from polymarket._internal.actions.orders import limit as sdk_limit  # noqa: E402
from polymarket._internal.actions.orders import market as sdk_market  # noqa: E402
from polymarket._internal.actions.orders.typed_data import (  # noqa: E402
    build_order_signature,
    build_order_typed_data,
)
from polymarket._internal.actions.orders.types import UnsignedOrder  # noqa: E402
from polymarket._internal.actions.perps import signing as sdk_perps  # noqa: E402
from polymarket._internal.actions.perps.trading import (  # noqa: E402
    to_command_body_op,
)
from polymarket._internal.actions.relayer import calls as sdk_calls  # noqa: E402
from polymarket._internal.actions.relayer.signing.deposit_wallet import (  # noqa: E402
    build_deposit_wallet_typed_data,
)
from polymarket._internal.l1_auth import build_api_key_auth_typed_data  # noqa: E402
from polymarket._internal.protocol import is_v2_position_id  # noqa: E402
from polymarket._internal.wallet import (  # noqa: E402
    wrap_deposit_wallet_session_signer_signature,
)

from mcp_server_polymarket.errors import InvalidRequest  # noqa: E402
from mcp_server_polymarket.signing import clob, eip712, orders, perps, relayer, wallets  # noqa: E402

KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TICKS = [Decimal(t) for t in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")]
rng = random.Random(20261111)


def random_price(tick: Decimal) -> Decimal:
    steps = int((1 - tick) / tick)
    return tick * rng.randint(1, max(steps - 1, 1))


def random_size() -> Decimal:
    return Decimal(rng.randint(1, 10**7)) / Decimal(10 ** rng.randint(0, 5))


def test_limit_amounts_agree():
    for _ in range(3000):
        tick = rng.choice(TICKS)
        price, size, side = random_price(tick), random_size(), rng.choice(["BUY", "SELL"])
        ours = orders.limit_amounts(side, price, size, tick)
        theirs = sdk_limit._compute_limit_order_amounts(
            price=price, size=size, side=side, tick_size=tick
        )
        assert ours == theirs, (side, price, size, tick)


def test_market_amounts_agree():
    for _ in range(3000):
        tick = rng.choice(TICKS)
        price, amount, side = random_price(tick), random_size(), rng.choice(["BUY", "SELL"])
        theirs = sdk_market._compute_market_order_amounts(
            amount=amount, price=price, side=side, tick_size=tick
        )
        try:
            ours = orders.market_amounts(side, amount, price, tick)
        except InvalidRequest:
            # We refuse what would round to zero or breach the bound; the SDK's protected path
            # refuses the same cases one level up.
            continue
        assert ours == theirs, (side, price, amount, tick)


def random_token() -> str:
    if rng.random() < 0.3:
        return str((rng.randint(1, 3) << 248) + rng.getrandbits(40))
    return str(rng.getrandbits(256))


def test_v2_detection_agrees():
    for _ in range(500):
        token = random_token()
        assert orders.is_v2_token(token) == is_v2_position_id(token)


@pytest.mark.parametrize("signature_type", [0, 1, 2, 3])
def test_order_typed_data_and_signatures_agree(signature_type):
    for _ in range(50):
        token = random_token()
        exchange, version = orders.exchange_for(token, neg_risk=rng.random() < 0.5)
        ours = orders.Order(
            token_id=token,
            side=rng.choice(["BUY", "SELL"]),
            maker="0x" + rng.getrandbits(160).to_bytes(20, "big").hex(),
            signer=ADDRESS,
            maker_amount=rng.getrandbits(40),
            taker_amount=rng.getrandbits(40),
            signature_type=signature_type,
            salt=rng.getrandbits(53),
            timestamp_ms=rng.getrandbits(42),
            exchange=exchange,
            version=version,
        )
        if signature_type == 3:
            ours = orders.Order(**{**ours.__dict__, "signer": ours.maker})
        theirs = UnsignedOrder(
            chain_id=137,
            builder=ours.builder,
            exchange_address=ours.exchange,
            expiration=0,
            maker=ours.maker,
            maker_amount=ours.maker_amount,
            metadata=ours.metadata,
            order_type="GTC",
            salt=ours.salt,
            side=ours.side,
            signature_type=signature_type,
            signer=ours.signer,
            taker_amount=ours.taker_amount,
            timestamp=ours.timestamp_ms,
            token_id=token,
            protocol_version=version,
        )
        typed = build_order_typed_data(theirs)
        assert eip712.digest(orders.order_typed_data(ours)) == eip712.digest(typed)
        raw = eip712.sign(KEY, typed)
        assert orders.sign_order(KEY, ours).signature == build_order_signature(theirs, raw)


def test_session_envelope_agrees():
    signature = "0x" + "11" * 65 + "22" * 100
    assert wallets.wrap_session_signature(ADDRESS, signature) == (
        wrap_deposit_wallet_session_signer_signature(ADDRESS, signature)
    )


def test_clob_auth_agrees():
    ours = clob.clob_auth_typed_data(ADDRESS, 1_794_000_000, 3)
    theirs = build_api_key_auth_typed_data(
        address=ADDRESS, chain_id=137, timestamp=1_794_000_000, nonce=3
    )
    assert eip712.digest(ours) == eip712.digest(theirs)


def test_deposit_wallet_batch_agrees():
    calls = [
        relayer.erc20_approve(wallets.PUSD, wallets.ROUTER_V2, 5),
        relayer.ctf_split(wallets.COLLATERAL_ADAPTER, "0x" + "ab" * 32, 1_000_000),
    ]
    wallet = "0x" + "c" * 40
    theirs = build_deposit_wallet_typed_data(
        wallet=wallet,
        calls=[sdk_calls.TransactionCall(to=c.target, data=c.data, value=c.value) for c in calls],
        nonce="4",
        deadline="99",
        chain_id=137,
    )
    assert eip712.digest(relayer.batch_typed_data(wallet, calls, 4, 99)) == eip712.digest(theirs)


def test_call_encoders_agree():
    condition = "0x" + "ab" * 32
    v2_condition = "0x" + "cd" * 31
    pairs = [
        (
            relayer.ctf_merge(wallets.COLLATERAL_ADAPTER, condition, 7),
            sdk_calls.ctf_merge_positions_call(
                target=wallets.COLLATERAL_ADAPTER,
                collateral=wallets.PUSD,
                condition_id=condition,
                amount=7,
            ),
        ),
        (
            relayer.router_split(wallets.ROUTER_V2, v2_condition, 9),
            sdk_calls.router_split_call(
                router=wallets.ROUTER_V2, condition_id=v2_condition, amount=9
            ),
        ),
        (
            relayer.router_merge(wallets.ROUTER_V2, v2_condition + "00", 9),
            sdk_calls.router_merge_call(
                router=wallets.ROUTER_V2, condition_id=v2_condition, amount=9
            ),
        ),
        (
            relayer.router_redeem(wallets.ROUTER_V2, v2_condition, 1, 9),
            sdk_calls.router_redeem_call(
                router=wallets.ROUTER_V2, condition_id=v2_condition, outcome_index=1, amount=9
            ),
        ),
        (
            relayer.authorize_session_signer("0x" + "c" * 40, ADDRESS, 123),
            sdk_calls.authorize_session_signer_call(
                wallet_address="0x" + "c" * 40, session_signer=ADDRESS, valid_until=123
            ),
        ),
        (
            relayer.revoke_session_signer("0x" + "c" * 40, ADDRESS),
            sdk_calls.revoke_session_signer_call(
                wallet_address="0x" + "c" * 40, session_signer=ADDRESS
            ),
        ),
    ]
    for ours, theirs in pairs:
        assert ours.data == theirs.data
        assert ours.target.lower() == str(theirs.to).lower()


def test_perps_bodies_and_hashes_agree():
    ops = [
        perps.create_orders(
            [
                perps.order_row(
                    perps.PerpsOrder(instrument_id=3, buy=True, quantity="1.5", price="100.1")
                ),
                perps.order_row(
                    perps.PerpsOrder(
                        instrument_id=3,
                        buy=False,
                        quantity="1.5",
                        reduce_only=True,
                        trigger=perps.Trigger("sl", "95", limit_price="94.5"),
                    )
                ),
            ],
            "order",
        ),
        perps.cancel_all(),
        perps.cancel_all(9),
        perps.auto_cancel(1_800_000_000_000),
        perps.update_leverages([(1, 5, True), (2, 3, False)]),
        perps.update_margin(4, "12.5"),
    ]
    for op in ops:
        assert perps.op_hash(op) == sdk_perps.hash_perps_op(op)
        assert perps.body_op(op) == to_command_body_op(op)
