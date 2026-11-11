from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.crypto import keccak

CHAIN_ID = 137


def digest(typed: dict[str, Any]) -> str:
    """The EIP-712 hash that gets signed."""
    signable = encode_typed_data(full_message=typed)
    return "0x" + keccak(b"\x19\x01" + signable.header + signable.body).hex()


def sign(private_key: str, typed: dict[str, Any]) -> str:
    signed = Account.from_key(private_key).sign_typed_data(full_message=typed)
    return "0x" + bytes(signed.signature).hex()


def recover(typed: dict[str, Any], signature: str) -> str:
    return Account.recover_message(encode_typed_data(full_message=typed), signature=signature)


def address_of(private_key: str) -> str:
    return Account.from_key(private_key).address


def new_key() -> tuple[str, str]:
    """A fresh secp256k1 key and its address."""
    account = Account.create()
    return "0x" + bytes(account.key).hex(), account.address
