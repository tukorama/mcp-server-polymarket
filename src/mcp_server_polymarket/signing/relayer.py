"""Deposit Wallet batches for the Relayer, and the contract calls they carry."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.signing.eip712 import CHAIN_ID, address_of, sign
from mcp_server_polymarket.signing.wallets import (
    DEPOSIT_WALLET_FACTORY,
    PERPS_DEPOSIT_CONTRACT,
    PUSD,
    wrap_session_signature,
)

MAX_UINT256 = (1 << 256) - 1
_ZERO32 = b"\0" * 32
_BINARY = [1, 2]


@dataclass(frozen=True)
class Call:
    target: str
    data: str
    value: int = 0


def _selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def _call(target: str, signature: str, types: list[str], values: list[Any]) -> Call:
    return Call(target=target, data="0x" + (_selector(signature) + abi_encode(types, values)).hex())


def _bytes32(condition_id: str) -> bytes:
    raw = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
    if len(raw) != 32:
        raise InvalidRequest("condition_id must be 32 bytes of hex.")
    return raw


def _bytes31(condition_id: str) -> bytes:
    hex_value = condition_id.lower().removeprefix("0x")
    if len(hex_value) == 64 and hex_value.endswith(("00", "01")):
        hex_value = hex_value[:-2]
    if len(hex_value) != 62:
        raise InvalidRequest("A Protocol V2 condition id is 31 bytes, or 32 with an outcome byte.")
    return bytes.fromhex(hex_value)


def _amount(value: int) -> int:
    if isinstance(value, bool) or not 0 < value <= MAX_UINT256:
        raise InvalidRequest("Amounts must be positive base units.")
    return value


def erc20_approve(token: str, spender: str, amount: int) -> Call:
    return _call(token, "approve(address,uint256)", ["address", "uint256"], [spender, amount])


def erc20_transfer(token: str, recipient: str, amount: int) -> Call:
    return _call(
        token, "transfer(address,uint256)", ["address", "uint256"], [recipient, _amount(amount)]
    )


def erc1155_set_approval_for_all(token: str, operator: str, approved: bool = True) -> Call:
    return _call(
        token, "setApprovalForAll(address,bool)", ["address", "bool"], [operator, approved]
    )


def ctf_split(target: str, condition_id: str, amount: int) -> Call:
    return _call(
        target,
        "splitPosition(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [PUSD, _ZERO32, _bytes32(condition_id), _BINARY, _amount(amount)],
    )


def ctf_merge(target: str, condition_id: str, amount: int) -> Call:
    return _call(
        target,
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [PUSD, _ZERO32, _bytes32(condition_id), _BINARY, _amount(amount)],
    )


def ctf_redeem(target: str, condition_id: str, collateral: str = PUSD) -> Call:
    return _call(
        target,
        "redeemPositions(address,bytes32,bytes32,uint256[])",
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral, _ZERO32, _bytes32(condition_id), _BINARY],
    )


def router_split(router: str, condition_id: str, amount: int) -> Call:
    return _call(
        router,
        "split(bytes31,uint256)",
        ["bytes31", "uint256"],
        [_bytes31(condition_id), _amount(amount)],
    )


def router_merge(router: str, condition_id: str, amount: int) -> Call:
    return _call(
        router,
        "merge(bytes31,uint256)",
        ["bytes31", "uint256"],
        [_bytes31(condition_id), _amount(amount)],
    )


def router_redeem(router: str, condition_id: str, outcome_index: int, amount: int) -> Call:
    if outcome_index not in (0, 1):
        raise InvalidRequest("outcome_index must be 0 (Yes) or 1 (No).")
    return _call(
        router,
        "redeem(bytes31,uint256,uint256)",
        ["bytes31", "uint256", "uint256"],
        [_bytes31(condition_id), outcome_index, _amount(amount)],
    )


def authorize_session_signer(wallet: str, session_signer: str, valid_until: int) -> Call:
    return _call(
        wallet,
        "authorizeSessionSigner(address,uint256)",
        ["address", "uint256"],
        [session_signer, valid_until],
    )


def revoke_session_signer(wallet: str, session_signer: str) -> Call:
    return _call(wallet, "revokeSessionSigner(address)", ["address"], [session_signer])


def perps_deposit(amount: int, to: str, token: str = PUSD) -> Call:
    return _call(
        PERPS_DEPOSIT_CONTRACT,
        "deposit(address,uint256,address)",
        ["address", "uint256", "address"],
        [token, _amount(amount), to],
    )


def batch_typed_data(
    wallet: str, calls: Sequence[Call], nonce: int, deadline: int
) -> dict[str, Any]:
    return {
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": wallet,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
        },
        "primaryType": "Batch",
        "message": {
            "wallet": wallet,
            "nonce": nonce,
            "deadline": deadline,
            "calls": [
                {"target": c.target, "value": c.value, "data": bytes.fromhex(c.data[2:])}
                for c in calls
            ],
        },
    }


def sign_batch(
    private_key: str,
    *,
    wallet: str,
    calls: Sequence[Call],
    nonce: int,
    deadline: int,
    session_key: bool = False,
) -> str:
    signature = sign(private_key, batch_typed_data(wallet, calls, nonce, deadline))
    if session_key:
        signature = wrap_session_signature(address_of(private_key), signature)
    return signature


def batch_wire(
    *,
    signer: str,
    wallet: str,
    calls: Sequence[Call],
    nonce: int,
    deadline: int,
    signature: str,
    metadata: str = "",
) -> dict[str, Any]:
    """The Relayer POST /submit body for a Deposit Wallet batch."""
    return {
        "type": "WALLET",
        "from": signer,
        "to": DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce),
        "signature": signature,
        "metadata": metadata,
        "depositWalletParams": {
            "depositWallet": wallet,
            "deadline": str(deadline),
            "calls": [{"target": c.target, "value": str(c.value), "data": c.data} for c in calls],
        },
    }
