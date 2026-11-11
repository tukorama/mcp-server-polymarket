"""Polymarket wallet kinds, contract addresses and address derivations (Polygon mainnet)."""

from enum import IntEnum

from eth_abi.abi import encode as abi_encode
from eth_abi.packed import encode_packed
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak


class WalletKind(IntEnum):
    """The order `signatureType` of each wallet kind."""

    EOA = 0
    POLY_PROXY = 1
    GNOSIS_SAFE = 2
    DEPOSIT_WALLET = 3


PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POSITION_MANAGER = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"
STANDARD_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
EXCHANGE_V3 = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
ROUTER_V2 = "0x12121212006e4CD160D18e3f00711DA5c3372600"
PERPS_DEPOSIT_CONTRACT = "0xDCa4af75705dbB50f62437045afF9921947917d2"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
DEPOSIT_WALLET_BEACON = "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a"
DEPOSIT_WALLET_UUPS_IMPLEMENTATION = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_IMPLEMENTATION = "0x44e999d5c2F66Ef0861317f9A4805AC2e90aEB4f"
SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
SAFE_INIT_CODE_HASH = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"

_PROXY_BYTECODE = (
    "3d3d606380380380913d393d73{factory}5af4602a57600080fd5b602d8060366000396000f3363d3d373d3d3d"
    "363d73{impl}5af43d82803e903d91602b57fd5bf352e831dd"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
_UUPS_CONST1 = bytes.fromhex("cc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3")
_UUPS_CONST2 = bytes.fromhex("5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076")
_UUPS_PREFIX = 0x61003D3D8160233D3973
_BEACON_CONST1 = bytes.fromhex("b3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3")
_BEACON_CONST2 = bytes.fromhex("1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c")
_BEACON_CONST3 = bytes.fromhex("60195155f3363d3d373d3d363d602036600436635c60da")
_BEACON_PREFIX = 0x6100523D8160233D3973
# ERC-6492 magic suffix, which marks a wrapped session-signer signature.
_SESSION_MAGIC = bytes.fromhex("6492" * 16)


def _hex(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith(("0x", "0X")) else value)


def _create2(factory: str, salt: bytes, code_hash: bytes) -> str:
    return to_checksum_address("0x" + keccak(b"\xff" + _hex(factory) + salt + code_hash)[12:].hex())


def derive_proxy_wallet(signer: str) -> str:
    code = bytes.fromhex(
        _PROXY_BYTECODE.format(
            factory=PROXY_FACTORY[2:].lower(), impl=PROXY_IMPLEMENTATION[2:].lower()
        )
    )
    salt = keccak(encode_packed(["address"], [to_checksum_address(signer)]))
    return _create2(PROXY_FACTORY, salt, keccak(code))


def derive_safe_wallet(signer: str) -> str:
    salt = keccak(abi_encode(["address"], [to_checksum_address(signer)]))
    return _create2(SAFE_FACTORY, salt, _hex(SAFE_INIT_CODE_HASH))


def _deposit_args(signer: str) -> bytes:
    return abi_encode(
        ["address", "bytes32"], [DEPOSIT_WALLET_FACTORY, _hex(signer).rjust(32, b"\0")]
    )


def derive_deposit_wallet(signer: str, *, legacy_uups: bool = False) -> str:
    """Deposit Wallets are beacon proxies; ones deployed before June 29, 2026 are UUPS."""
    args = _deposit_args(signer)
    prefix_base, target = (
        (_UUPS_PREFIX, DEPOSIT_WALLET_UUPS_IMPLEMENTATION)
        if legacy_uups
        else (_BEACON_PREFIX, DEPOSIT_WALLET_BEACON)
    )
    prefix = (prefix_base + (len(args) << 56)).to_bytes(10, "big")
    if legacy_uups:
        code = prefix + _hex(target) + bytes.fromhex("6009") + _UUPS_CONST2 + _UUPS_CONST1 + args
    else:
        code = prefix + _hex(target) + _BEACON_CONST3 + _BEACON_CONST2 + _BEACON_CONST1 + args
    return _create2(DEPOSIT_WALLET_FACTORY, keccak(args), keccak(code))


def classify_wallet(signer: str, wallet: str) -> WalletKind | None:
    """The kind of `wallet` if `signer` owns it; None if it cannot be derived from the signer."""
    signer, wallet = to_checksum_address(signer), to_checksum_address(wallet)
    if wallet == signer:
        return WalletKind.EOA
    if wallet in (derive_deposit_wallet(signer), derive_deposit_wallet(signer, legacy_uups=True)):
        return WalletKind.DEPOSIT_WALLET
    if wallet == derive_proxy_wallet(signer):
        return WalletKind.POLY_PROXY
    if wallet == derive_safe_wallet(signer):
        return WalletKind.GNOSIS_SAFE
    return None


def wrap_session_signature(session_signer: str, signature: str) -> str:
    """A Deposit Wallet session key's signature: (signer id, 0, signature) plus the magic."""
    payload = abi_encode(
        ["bytes32", "bytes32", "bytes"],
        [_hex(session_signer).rjust(32, b"\0"), b"\0" * 32, _hex(signature)],
    )
    return "0x" + (payload + _SESSION_MAGIC).hex()
