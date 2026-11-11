"""Signatures pinned to the official SDK's golden vectors (polymarket-client, MIT; see NOTICE).

Each vector there was generated from Polymarket's TypeScript implementation, which the
servers verify against, so matching them byte for byte is what makes our requests valid.
"""

from decimal import Decimal

import pytest

pytest.importorskip("eth_account")

from mcp_server_polymarket.errors import InvalidRequest  # noqa: E402
from mcp_server_polymarket.signing import clob, eip712, orders, perps, relayer, wallets  # noqa: E402

ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# Orders


def order(signature_type: int) -> orders.Order:
    wallet = "0x57ffbc34de23124faeb8387fcd689d314e57accd"
    return orders.Order(
        token_id="1",
        side="BUY",
        maker=wallet,
        signer=wallet,
        maker_amount=1_000_000,
        taker_amount=500_000,
        signature_type=signature_type,
        salt=1,
        timestamp_ms=0,
        exchange="0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        version="2",
    )


@pytest.mark.parametrize(
    ("signature_type", "digest"),
    [
        (0, "0xa511aa6ff25ecfc36227717b080802396f686a8cf012fe38b4c30153b91be7a5"),
        (1, "0x6c1e2461f1aceb08efa25fbd99c6087f86ee7e67dab570dc7a0aad194bd9c653"),
        (3, "0x1b9566eedd9589a73275df23a3a9d9e2e9897e76d31cd46d436f1b824d161b33"),
    ],
)
def test_order_digests_match_the_sdk(signature_type, digest):
    assert eip712.digest(orders.order_typed_data(order(signature_type))) == digest


def test_order_signatures_recover_to_the_signer():
    for signature_type in (0, 3):
        typed = orders.order_typed_data(order(signature_type))
        signature = eip712.sign(ANVIL_KEY, typed)
        assert eip712.recover(typed, signature) == ANVIL_ADDRESS


def test_deposit_wallet_signature_carries_the_erc7739_trailer():
    signed = orders.sign_order(ANVIL_KEY, order(3))
    body = signed.signature[2:]
    trailer = body[65 * 2 + 64 * 2 :]
    type_string = (
        "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
        "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
        "uint256 timestamp,bytes32 metadata,bytes32 builder)"
    )
    assert trailer == type_string.encode().hex() + "00ba"


def test_session_key_signature_is_wrapped():
    signed = orders.sign_order(ANVIL_KEY, order(3), session_key=True)
    assert signed.signature.endswith("6492" * 16)
    assert ANVIL_ADDRESS[2:].lower() in signed.signature[:66].lower()


def test_eoa_and_proxy_signatures_are_plain():
    signed = orders.sign_order(ANVIL_KEY, order(0))
    assert len(signed.signature) == 2 + 65 * 2


def test_v2_tokens_route_to_exchange_v3():
    v2 = str((1 << 248) + 5)  # module byte set, reserved bits clear
    clob_token = "55764212211467781322980371912612507865974994976253196346176314491480419639168"
    assert orders.exchange_for(v2, neg_risk=False) == (wallets.EXCHANGE_V3, "3")
    assert orders.exchange_for(clob_token, neg_risk=False) == (wallets.STANDARD_EXCHANGE, "2")
    assert orders.exchange_for(clob_token, neg_risk=True) == (wallets.NEG_RISK_EXCHANGE, "2")


def test_deposit_wallet_orders_sign_as_the_wallet():
    built = orders.build_order(
        token_id="1",
        side="SELL",
        maker_amount=1,
        taker_amount=1,
        wallet="0x" + "a" * 40,
        signer=ANVIL_ADDRESS,
        kind=wallets.WalletKind.DEPOSIT_WALLET,
        neg_risk=False,
        salt=1,
        timestamp_ms=1,
    )
    assert built.signer == built.maker == "0x" + "a" * 40
    proxy = orders.build_order(
        token_id="1",
        side="SELL",
        maker_amount=1,
        taker_amount=1,
        wallet="0x" + "a" * 40,
        signer=ANVIL_ADDRESS,
        kind=wallets.WalletKind.POLY_PROXY,
        neg_risk=False,
        salt=1,
        timestamp_ms=1,
    )
    assert proxy.signer == ANVIL_ADDRESS


def test_order_wire_matches_the_api():
    signed = orders.sign_order(ANVIL_KEY, order(0))
    body = orders.order_wire(signed, owner="key-uuid", order_type="GTC", post_only=True)
    assert body["owner"] == "key-uuid" and body["deferExec"] is False and body["postOnly"] is True
    assert body["order"]["salt"] == 1 and body["order"]["makerAmount"] == "1000000"
    assert body["order"]["timestamp"] == "0" and body["order"]["expiration"] == "0"


# Amounts


@pytest.mark.parametrize(
    ("side", "price", "size", "tick", "expected"),
    [
        ("BUY", "0.5", "10", "0.01", (5_000_000, 10_000_000)),
        ("SELL", "0.5", "10", "0.01", (10_000_000, 5_000_000)),
        ("BUY", "0.56", "21.04", "0.01", (11_782_400, 21_040_000)),
        ("BUY", "0.003", "100.129", "0.001", (300_360, 100_120_000)),
        ("SELL", "0.0025", "7", "0.0025", (7_000_000, 17_500)),
    ],
)
def test_limit_amounts(side, price, size, tick, expected):
    assert orders.limit_amounts(side, Decimal(price), Decimal(size), Decimal(tick)) == expected


def test_market_buy_keeps_the_bound():
    maker, taker = orders.market_amounts("BUY", Decimal("10"), Decimal("0.33"), Decimal("0.01"))
    assert maker == 10_000_000
    assert Decimal(maker) / Decimal(taker) >= Decimal("0.33")
    assert Decimal(maker) / Decimal(taker) < Decimal("0.3301")


def test_market_sell_amounts():
    assert orders.market_amounts("SELL", Decimal("10"), Decimal("0.4"), Decimal("0.01")) == (
        10_000_000,
        4_000_000,
    )


def test_off_tick_prices_are_refused_not_rounded():
    with pytest.raises(InvalidRequest, match="tick size"):
        orders.check_price(Decimal("0.555"), Decimal("0.01"))
    with pytest.raises(InvalidRequest, match="between"):
        orders.check_price(Decimal("1"), Decimal("0.01"))
    assert orders.check_price(Decimal("0.55"), Decimal("0.01")) == Decimal("0.55")


def test_unknown_tick_size_is_refused():
    with pytest.raises(InvalidRequest):
        orders.rounding_for(Decimal("0.02"))


# Wallets


def test_wallet_derivations_match_the_sdk():
    signer = "0x0000000000000000000000000000000000000001"
    assert wallets.derive_deposit_wallet(signer).lower() == (
        "0x94bf330955a0b957662feaf878de77bf25f76cd9"
    )
    assert wallets.derive_deposit_wallet(signer, legacy_uups=True).lower() == (
        "0x57ffbc34de23124faeb8387fcd689d314e57accd"
    )
    assert wallets.derive_proxy_wallet(signer).lower() == (
        "0x7754536ecd85c00b2e0cf9c1aa679340d8550756"
    )
    assert wallets.derive_safe_wallet(signer).lower() == (
        "0x766b6851a199bf91ae3fa13b1cfac5187355118f"
    )


def test_classify_wallet_never_guesses():
    signer = "0x0000000000000000000000000000000000000001"
    assert wallets.classify_wallet(signer, signer) == wallets.WalletKind.EOA
    assert (
        wallets.classify_wallet(signer, "0x94bf330955a0b957662feaf878de77bf25f76cd9")
        == wallets.WalletKind.DEPOSIT_WALLET
    )
    assert (
        wallets.classify_wallet(signer, "0x7754536ecd85c00b2e0cf9c1aa679340d8550756")
        == wallets.WalletKind.POLY_PROXY
    )
    assert (
        wallets.classify_wallet(signer, "0x766b6851a199bf91ae3fa13b1cfac5187355118f")
        == wallets.WalletKind.GNOSIS_SAFE
    )
    assert wallets.classify_wallet(signer, "0x" + "2" * 40) is None


# Relayer calls


USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CONDITION = "0x" + "11" * 32


def test_call_encoders_match_the_sdk():
    assert relayer.erc20_approve(USDC, wallets.CONDITIONAL_TOKENS, relayer.MAX_UINT256).data == (
        "0x095ea7b3"
        "0000000000000000000000004d97dcd97ec945f40cf65f87097ace5ea0476045"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    assert relayer.erc20_transfer(USDC, "0x000000000000000000000000000000000000dEaD", 1).data == (
        "0xa9059cbb"
        "000000000000000000000000000000000000000000000000000000000000dead"
        "0000000000000000000000000000000000000000000000000000000000000001"
    )
    approval = relayer.erc1155_set_approval_for_all(
        wallets.CONDITIONAL_TOKENS, wallets.NEG_RISK_ADAPTER
    )
    assert approval.data == (
        "0xa22cb465"
        "000000000000000000000000d91e80cf2e7be2e162c6513ced06f1dd0da35296"
        "0000000000000000000000000000000000000000000000000000000000000001"
    )


def test_ctf_split_and_redeem_layouts():
    split = relayer.ctf_split(wallets.COLLATERAL_ADAPTER, CONDITION, 1_000_000).data
    assert split.startswith("0x72ce4275")
    assert split.endswith(
        "00000000000000000000000000000000000000000000000000000000000f4240"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "0000000000000000000000000000000000000000000000000000000000000001"
        "0000000000000000000000000000000000000000000000000000000000000002"
    )
    redeem = relayer.ctf_redeem(wallets.CONDITIONAL_TOKENS, CONDITION, collateral=USDC).data
    assert redeem == (
        "0x01b7037c"
        "0000000000000000000000002791bca1f2de4661ed88a30c99a7a9449aa84174"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "1111111111111111111111111111111111111111111111111111111111111111"
        "0000000000000000000000000000000000000000000000000000000000000080"
        "0000000000000000000000000000000000000000000000000000000000000002"
        "0000000000000000000000000000000000000000000000000000000000000001"
        "0000000000000000000000000000000000000000000000000000000000000002"
    )


def test_batch_signature_recovers_and_wires():
    calls = [relayer.erc20_approve(wallets.PUSD, wallets.ROUTER_V2, 5)]
    wallet = "0x" + "b" * 40
    typed = relayer.batch_typed_data(wallet, calls, nonce=3, deadline=100)
    signature = relayer.sign_batch(ANVIL_KEY, wallet=wallet, calls=calls, nonce=3, deadline=100)
    assert eip712.recover(typed, signature) == ANVIL_ADDRESS
    body = relayer.batch_wire(
        signer=ANVIL_ADDRESS, wallet=wallet, calls=calls, nonce=3, deadline=100, signature=signature
    )
    assert body["type"] == "WALLET" and body["to"] == wallets.DEPOSIT_WALLET_FACTORY
    assert body["depositWalletParams"]["calls"][0]["value"] == "0"


# CLOB L1


def test_clob_auth_headers_recover_to_the_signer():
    headers = clob.l1_headers(ANVIL_KEY, timestamp=1_700_000_000)
    typed = clob.clob_auth_typed_data(ANVIL_ADDRESS, 1_700_000_000)
    assert headers["POLY_ADDRESS"] == ANVIL_ADDRESS and headers["POLY_NONCE"] == "0"
    assert eip712.recover(typed, headers["POLY_SIGNATURE"]) == ANVIL_ADDRESS


# Perps

PERPS_KEY = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PERPS_VECTORS = [
    (
        ["createOrders", [[1, True, "0.5", "10", "gtc", False, None, None, None]]],
        12345,
        1751500000000,
        "0x8004f264b573f0d5edd3377ef127f251a2b11e0b9463c5fb5f1be3b42c94336a",
        "0xdd933bdada3c14c01dbe48fc02d470f423f0e0ef0897052602c964e20f82c709"
        "7c1ea1033e1e939a0633db606781f3b7160603337ab6201755cb191e99bc2d9b1c",
    ),
    (
        [
            "createOrders",
            [
                [
                    42,
                    False,
                    None,
                    "2.5",
                    "ioc",
                    False,
                    None,
                    "aabbccddeeff00112233445566778899",
                    None,
                ]
            ],
        ],
        4294967295,
        1751500000001,
        "0x3b358621af8f4d297ea0c78b9d40aa9d33b84de0f9a619726f8f9e2f305c0157",
        "0x1b728cc550e2691bab29ccf183990ab53786fe86b28a5659560f6c7e92faa8f6"
        "0535f2a0731265f0bdb9bf9c3649ada69857dfe33c887ff9b3fc2794d605adde1b",
    ),
    (
        [
            "createOrders",
            [
                [7, True, "100", "3", "gtc", False, None, None, None],
                [7, False, None, "3", None, False, True, None, [True, "200", "tp"]],
                [7, False, "50", "3", None, False, True, None, [None, "49", "sl"]],
            ],
            "order",
        ],
        1,
        1751500000002,
        "0x34b31ff82aa6d39bd6ee54a7781287d6e6dffcc0cc7dcc981f70e381c7d8a641",
        "0xfa7438604c02142ed9a0e3612a38d3b0b49eb21141515ac9a30be03b9f4422ea"
        "5b598c70b2e733f6564eb322ac6ec5293c656c86426f389e9c5454e33d086cb21b",
    ),
    (
        ["cancelOrders", [11, 22, 33]],
        999,
        1751500000003,
        "0x13f4659952efbcd144324c0a1c50cb75069635b2bbf28d3cc1e51b6e176a7617",
        "0xb97be11bcffaca178b29ecc42cabc732d0f436fad42a42e3a63f32131b347be5"
        "11d452b14a81bdefc7cfda84bf83c94afedfb16fd8df16883cf6e261e411dfe91c",
    ),
    (
        ["cancelOrdersCOID", ["aabbccddeeff00112233445566778899"]],
        1000,
        1751500000004,
        "0xdf8f1749d1b75392360c1117dcf2e0f251b0197b4db21c37a4e7d2451bc7cc95",
        "0xf3b23b2357864efb2cd1ac128a9b6601c85a7894d9be000243f253a37f778c39"
        "525d0359d95e01350d5bfca6ebf4b84bb5735acfbcb8c8fb90978bb9f371d58a1c",
    ),
    (
        ["updateLeverage", [3, 20, True]],
        7,
        1751500000005,
        "0x1daa002ce2f4e42ab0ef2d3c489b2386cccac2cbb861bdd7695761d4d93dd410",
        "0x5b81c182f89acc819488f30daa7a6fdb7563b0f65b2b404e9ece8a72d8077c3f"
        "6bf328900ed6594d56605e564fa1e4469c8be670ac86015ab160bd78a97ea2bd1b",
    ),
    (
        ["autoCancel", [1767000045000]],
        12345,
        1751500000000,
        "0x8d16f1dbf6be71cea6ad70c5b09f028dd1b451cebb2c7dd20c82dbf1022440ba",
        "0x9019fb6edfc125e19635ac74d50efc1c863bc26e4d395ee76a8e26d7df001144"
        "24d5a338540f9b7ecb46fb3825ad662da852856257829c3664ce84d02bfcdda81c",
    ),
    (
        ["autoCancel", [0]],
        12345,
        1751500000000,
        "0x569d0f5365d9bb75e7f2218717986828ea553b937a4c901528c70fa173fa539e",
        "0x188bebb866e6f11dd439ea12e2869eb9308fb6a9c6c9da09ece8a74467d7c4ac"
        "582c4a9929094af1d994467e40c81143de007b6b1499286b265512fdde82b29a1c",
    ),
    (
        ["updateMargin", [3, "-25.000000000000000001"]],
        8,
        1751500000006,
        "0x1bceee4cd50ae288f5ae7e993deeea51fe9dd41b5eff27840455ed72e72d4f4c",
        "0xfd6ec2ee47423b3f87adfc64eadd9764180e05102efadb112403016a446d072d"
        "497ec201b2df1e7944f465c9c734d3698b80dbe51a42eb8167a7a54e4249dfc81b",
    ),
    (
        ["deleteProxy", ["0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"]],
        55,
        1751500000006,
        "0x7274223decc60adbbe3ba91ceade2e6cd7901ab7ba506d07fb2d0dbef83e30d4",
        "0x2f6400725b8af890a69b8c47fe9d2b100d15714a73a13a92b15187909d1c1080"
        "51dc906b8ec65cfc4d161c579df7b2cb9c6f7efea30dc238cfd7526fd3b797c01c",
    ),
]


@pytest.mark.parametrize(("op", "salt", "ts", "data", "signature"), PERPS_VECTORS)
def test_perps_ops_match_the_sdk(op, salt, ts, data, signature):
    assert perps.op_hash(op) == data
    assert perps.sign_op(PERPS_KEY, op, salt=salt, ts_ms=ts) == signature


def test_perps_builders_reproduce_the_vectors():
    gtc = perps.order_row(perps.PerpsOrder(instrument_id=1, buy=True, quantity="10", price="0.5"))
    assert gtc == PERPS_VECTORS[0][0][1][0]
    take_profit = perps.order_row(
        perps.PerpsOrder(
            instrument_id=7,
            buy=False,
            quantity="3",
            reduce_only=True,
            trigger=perps.Trigger("tp", "200"),
        )
    )
    stop_loss = perps.order_row(
        perps.PerpsOrder(
            instrument_id=7,
            buy=False,
            quantity="3",
            reduce_only=True,
            trigger=perps.Trigger("sl", "49", limit_price="50"),
        )
    )
    entry = perps.order_row(perps.PerpsOrder(instrument_id=7, buy=True, quantity="3", price="100"))
    assert perps.create_orders([entry, take_profit, stop_loss], "order") == PERPS_VECTORS[2][0]
    assert perps.update_leverage(3, 20, True) == PERPS_VECTORS[5][0]
    assert perps.delete_proxy("0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc") == PERPS_VECTORS[9][0]


def test_perps_request_body_mirrors_the_signed_op():
    op = PERPS_VECTORS[2][0]
    body = perps.signed_request(PERPS_KEY, op, salt=1, ts_ms=1751500000002, exp_ms=1751500005000)
    assert body["sig"] == PERPS_VECTORS[2][4]
    assert body["op"]["grp"] == "order"
    assert body["op"]["args"][1] == {
        "iid": 7,
        "buy": False,
        "po": False,
        "qty": "3",
        "ro": True,
        "tr": {"tpsl": "tp", "trp": "200", "market": True},
    }
    assert body["op"]["args"][2]["p"] == "50" and "market" not in body["op"]["args"][2]["tr"]
    assert body["exp"] == 1751500005000
    assert perps.body_op(perps.cancel_all(3)) == {"type": "cancelAll", "args": {"iid": 3}}
    assert perps.body_op(perps.update_margin(3, "-1")) == {
        "type": "updateMargin",
        "args": {"amt": "-1", "iid": 3},
    }


def test_create_proxy_and_withdraw_match_the_sdk():
    proxy = eip712.sign(
        PERPS_KEY,
        perps.create_proxy_typed_data(
            "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc", 1752000000000, 4242, 1751500000007
        ),
    )
    assert proxy == (
        "0x64e2d5aef2aeb5e58072ebb63f8419ba0f89d1de34635a007ef648db9e6ac6f3"
        "01997797def6e6a5b118f09fb1da0e3f58a55c34a871968e13227ae40eabf9341c"
    )
    withdraw = eip712.sign(
        PERPS_KEY,
        perps.withdraw_typed_data(
            account="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            token=wallets.PUSD,
            amount=10_000_000,
            to="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
            salt=77,
            ts_s=1751500000,
        ),
    )
    assert withdraw == (
        "0x75b6d0d9572921ed71d2a13fd3585dbd34326d9b503edaca08e3bf72dcd7bb43"
        "7fe67378df001fc9e4352063a87d9e0a690599bf0ed6849ec00660e6f28cfa1c1b"
    )
