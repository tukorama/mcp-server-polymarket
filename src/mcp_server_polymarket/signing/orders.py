"""CLOB orders: amounts from price and size, the EIP-712 Order, and its signature."""

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.protocol import is_v2_token
from mcp_server_polymarket.signing.eip712 import CHAIN_ID, address_of, sign
from mcp_server_polymarket.signing.wallets import (
    EXCHANGE_V3,
    NEG_RISK_EXCHANGE,
    STANDARD_EXCHANGE,
    WalletKind,
    wrap_session_signature,
)

Side = Literal["BUY", "SELL"]
ZERO32 = "0x" + "00" * 32
COLLATERAL_DECIMALS = 6
_NAME = "Polymarket CTF Exchange"
_ORDER_TYPE = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,"
    "uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,"
    "bytes32 builder)"
)
_DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
_ORDER_FIELDS = [
    {"name": "salt", "type": "uint256"},
    {"name": "maker", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "tokenId", "type": "uint256"},
    {"name": "makerAmount", "type": "uint256"},
    {"name": "takerAmount", "type": "uint256"},
    {"name": "side", "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
    {"name": "timestamp", "type": "uint256"},
    {"name": "metadata", "type": "bytes32"},
    {"name": "builder", "type": "bytes32"},
]
_TYPED_DATA_SIGN_FIELDS = [
    {"name": "contents", "type": "Order"},
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
    {"name": "salt", "type": "bytes32"},
]


@dataclass(frozen=True)
class Rounding:
    price: int
    size: int
    amount: int


ROUNDING = {
    Decimal("0.1"): Rounding(price=1, size=2, amount=3),
    Decimal("0.01"): Rounding(price=2, size=2, amount=4),
    Decimal("0.005"): Rounding(price=3, size=2, amount=5),
    Decimal("0.0025"): Rounding(price=4, size=2, amount=6),
    Decimal("0.001"): Rounding(price=3, size=2, amount=5),
    Decimal("0.0001"): Rounding(price=4, size=2, amount=6),
}
FINEST_TICK = min(ROUNDING)


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _round(value: Decimal, places: int, mode: str) -> Decimal:
    if decimal_places(value) <= places:
        return value
    return value.quantize(Decimal(10) ** -places, rounding=mode)


def base_units(value: Decimal) -> int:
    return int((value * 10**COLLATERAL_DECIMALS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def rounding_for(tick_size: Decimal) -> Rounding:
    config = ROUNDING.get(tick_size.normalize())
    if config is None:
        config = next((c for t, c in ROUNDING.items() if t == tick_size), None)
    if config is None:
        raise InvalidRequest(f"Unsupported tick size {tick_size}.")
    return config


def check_price(price: Decimal, tick_size: Decimal, field: str = "price") -> Decimal:
    """Prices must sit on the market's tick grid, inside (0, 1). They are never rounded."""
    if price < tick_size or price > 1 - tick_size:
        raise InvalidRequest(
            f"{field} must be between {tick_size} and {1 - tick_size} for this market."
        )
    if decimal_places(price) > decimal_places(tick_size) or price % tick_size != 0:
        raise InvalidRequest(
            f"{field} {price} is not on this market's tick size {tick_size}.",
            hint="Use a multiple of the tick size; get it from get_quotes.",
        )
    return price


def _round_amount(value: Decimal, places: int) -> Decimal:
    if decimal_places(value) <= places:
        return value
    value = _round(value, places + 4, ROUND_CEILING)
    if decimal_places(value) > places:
        value = _round(value, places, ROUND_FLOOR)
    return value


def limit_amounts(side: Side, price: Decimal, size: Decimal, tick_size: Decimal) -> tuple[int, int]:
    """(makerAmount, takerAmount) in base units. A BUY offers pUSD; a SELL offers shares."""
    config = rounding_for(tick_size)
    shares = _round(size, config.size, ROUND_FLOOR)
    value = _round_amount(shares * price, config.amount)
    if side == "BUY":
        return base_units(value), base_units(shares)
    return base_units(shares), base_units(value)


def market_amounts(
    side: Side, amount: Decimal, bound: Decimal, tick_size: Decimal
) -> tuple[int, int]:
    """A market order: BUY spends `amount` pUSD up to price `bound`; SELL sells `amount`
    shares down to price `bound`."""
    config = rounding_for(tick_size)
    price = _round(bound, config.price, ROUND_FLOOR)
    maker = _round(amount, config.size, ROUND_FLOOR)
    taker = maker / price if side == "BUY" else maker * price
    if decimal_places(taker) > config.amount:
        taker = _round(taker, config.amount + 4, ROUND_CEILING)
        if decimal_places(taker) > config.amount:
            # Round down: for a BUY the exchange prices the order at maker / taker, so fewer
            # shares keeps the price at or just above the bound and an ask there can fill.
            taker = _round(taker, config.amount, ROUND_FLOOR)
    maker_units, taker_units = base_units(maker), base_units(taker)
    if maker_units <= 0 or taker_units <= 0:
        raise InvalidRequest("The order amount rounds to zero.", hint="Increase the amount.")
    if side == "BUY":
        # Never let the encoded price reach the next tick on the finest grid, or the order could
        # fill above the bound.
        ceiling = price + FINEST_TICK
        if Decimal(maker_units) >= Decimal(taker_units) * ceiling:
            raise InvalidRequest(
                "This amount cannot keep the price at or below the bound at this precision.",
                hint="Increase the amount or choose another max_price.",
            )
    return maker_units, taker_units


def exchange_for(token_id: str, *, neg_risk: bool) -> tuple[str, Literal["2", "3"]]:
    """The verifying contract and domain version an order on this token is signed for."""
    if is_v2_token(token_id):
        return EXCHANGE_V3, "3"
    return (NEG_RISK_EXCHANGE if neg_risk else STANDARD_EXCHANGE), "2"


@dataclass(frozen=True)
class Order:
    token_id: str
    side: Side
    maker: str
    signer: str
    maker_amount: int
    taker_amount: int
    signature_type: int
    salt: int
    timestamp_ms: int
    exchange: str
    version: Literal["2", "3"]
    expiration: int = 0
    builder: str = ZERO32
    metadata: str = ZERO32
    signature: str | None = None


def build_order(
    *,
    token_id: str,
    side: Side,
    maker_amount: int,
    taker_amount: int,
    wallet: str,
    signer: str,
    kind: WalletKind,
    neg_risk: bool,
    salt: int,
    timestamp_ms: int,
    expiration: int = 0,
    builder: str | None = None,
) -> Order:
    exchange, version = exchange_for(token_id, neg_risk=neg_risk)
    return Order(
        token_id=token_id,
        side=side,
        maker=wallet,
        # A Deposit Wallet signs as itself (ERC-1271); other wallets name their owner.
        signer=wallet if kind == WalletKind.DEPOSIT_WALLET else signer,
        maker_amount=maker_amount,
        taker_amount=taker_amount,
        signature_type=int(kind),
        salt=salt,
        timestamp_ms=timestamp_ms,
        exchange=exchange,
        version=version,
        expiration=expiration,
        builder=builder or ZERO32,
    )


def _message(order: Order) -> dict[str, Any]:
    return {
        "salt": order.salt,
        "maker": order.maker,
        "signer": order.signer,
        "tokenId": int(order.token_id),
        "makerAmount": order.maker_amount,
        "takerAmount": order.taker_amount,
        "side": 0 if order.side == "BUY" else 1,
        "signatureType": order.signature_type,
        "timestamp": order.timestamp_ms,
        "metadata": order.metadata,
        "builder": order.builder,
    }


def _domain(order: Order) -> dict[str, Any]:
    return {
        "name": _NAME,
        "version": order.version,
        "chainId": CHAIN_ID,
        "verifyingContract": order.exchange,
    }


def order_typed_data(order: Order) -> dict[str, Any]:
    if order.signature_type != WalletKind.DEPOSIT_WALLET:
        return {
            "types": {"EIP712Domain": _DOMAIN_FIELDS, "Order": _ORDER_FIELDS},
            "primaryType": "Order",
            "domain": _domain(order),
            "message": _message(order),
        }
    # ERC-7739: the wallet's owner signs the order nested in the wallet's own domain.
    return {
        "types": {
            "EIP712Domain": _DOMAIN_FIELDS,
            "Order": _ORDER_FIELDS,
            "TypedDataSign": _TYPED_DATA_SIGN_FIELDS,
        },
        "primaryType": "TypedDataSign",
        "domain": _domain(order),
        "message": {
            "contents": _message(order),
            "name": "DepositWallet",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": order.signer,
            "salt": ZERO32,
        },
    }


def _hex(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def erc7739_trailer(order: Order) -> str:
    separator = keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                keccak(_DOMAIN_TYPE.encode()),
                keccak(_NAME.encode()),
                keccak(order.version.encode()),
                CHAIN_ID,
                order.exchange,
            ],
        )
    )
    message = _message(order)
    contents = keccak(
        abi_encode(
            ["bytes32", "uint256", "address", "address", "uint256", "uint256", "uint256", "uint8"]
            + ["uint8", "uint256", "bytes32", "bytes32"],
            [
                keccak(_ORDER_TYPE.encode()),
                message["salt"],
                message["maker"],
                message["signer"],
                message["tokenId"],
                message["makerAmount"],
                message["takerAmount"],
                message["side"],
                message["signatureType"],
                message["timestamp"],
                _hex(order.metadata),
                _hex(order.builder),
            ],
        )
    )
    return separator.hex() + contents.hex() + _ORDER_TYPE.encode().hex() + f"{len(_ORDER_TYPE):04x}"


def sign_order(private_key: str, order: Order, *, session_key: bool = False) -> Order:
    """Sign as the wallet owner, or as an authorised Deposit Wallet session key."""
    signature = sign(private_key, order_typed_data(order))
    if order.signature_type == WalletKind.DEPOSIT_WALLET:
        signature += erc7739_trailer(order)
        if session_key:
            signature = wrap_session_signature(address_of(private_key), signature)
    return replace(order, signature=signature)


def order_wire(
    order: Order, *, owner: str, order_type: str, post_only: bool = False
) -> dict[str, Any]:
    """The POST /order body. Amounts and times are strings; salt is a JSON number."""
    if order.signature is None:
        raise ValueError("order is not signed")
    body: dict[str, Any] = {
        "deferExec": False,
        "order": {
            "builder": order.builder,
            "expiration": str(order.expiration),
            "maker": order.maker,
            "makerAmount": str(order.maker_amount),
            "metadata": order.metadata,
            "salt": order.salt,
            "side": order.side,
            "signature": order.signature,
            "signatureType": order.signature_type,
            "signer": order.signer,
            "takerAmount": str(order.taker_amount),
            "timestamp": str(order.timestamp_ms),
            "tokenId": order.token_id,
        },
        "orderType": order_type,
        "owner": owner,
    }
    if post_only:
        body["postOnly"] = True
    return body
