"""Perps signed operations.

A command is a positional tuple (`["createOrders", [[iid, buy, ...]]]`). With `None` entries
removed it is MessagePack-encoded and hashed, and that hash is signed as EIP-712
`Op(data, salt, ts)`. The JSON body sent alongside names the same values with keys.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

import msgpack
from eth_utils.crypto import keccak

from mcp_server_polymarket.signing.eip712 import CHAIN_ID, sign
from mcp_server_polymarket.signing.wallets import PERPS_DEPOSIT_CONTRACT

_DOMAIN = {"name": "Polymarket", "version": "1", "chainId": CHAIN_ID}
_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
]


def compact(value: Any) -> Any:
    """Drop None entries from lists, recursively, as the server does before hashing."""
    if isinstance(value, list | tuple):
        return [compact(item) for item in cast("Sequence[Any]", value) if item is not None]
    return value


def op_hash(op: Sequence[Any]) -> str:
    packed = cast("bytes", cast("Any", msgpack).packb(compact(list(op))))
    return "0x" + keccak(packed).hex()


def op_typed_data(op: Sequence[Any], *, salt: int, ts_ms: int) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": _DOMAIN_FIELDS,
            "Op": [
                {"name": "data", "type": "bytes32"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "Op",
        "domain": dict(_DOMAIN),
        "message": {"data": op_hash(op), "salt": salt, "ts": ts_ms},
    }


def sign_op(private_key: str, op: Sequence[Any], *, salt: int, ts_ms: int) -> str:
    return sign(private_key, op_typed_data(op, salt=salt, ts_ms=ts_ms))


def create_proxy_typed_data(proxy: str, exp_ms: int, salt: int, ts_ms: int) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": _DOMAIN_FIELDS,
            "CreateProxy": [
                {"name": "addr", "type": "address"},
                {"name": "exp", "type": "uint64"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "CreateProxy",
        "domain": dict(_DOMAIN),
        "message": {"addr": proxy, "exp": exp_ms, "salt": salt, "ts": ts_ms},
    }


def withdraw_typed_data(
    account: str, token: str, amount: int, to: str, salt: int, ts_s: int
) -> dict[str, Any]:
    """Withdrawals are checked on chain, so `ts` is in seconds and the contract is named."""
    return {
        "types": {
            "EIP712Domain": [*_DOMAIN_FIELDS, {"name": "verifyingContract", "type": "address"}],
            "Withdraw": [
                {"name": "account", "type": "address"},
                {"name": "token", "type": "address"},
                {"name": "amount", "type": "uint256"},
                {"name": "fee", "type": "uint256"},
                {"name": "to", "type": "address"},
                {"name": "salt", "type": "uint64"},
                {"name": "ts", "type": "uint64"},
            ],
        },
        "primaryType": "Withdraw",
        "domain": {**_DOMAIN, "verifyingContract": PERPS_DEPOSIT_CONTRACT},
        "message": {
            "account": account,
            "token": token,
            "amount": amount,
            "fee": 0,
            "to": to,
            "salt": salt,
            "ts": ts_s,
        },
    }


# Command builders. Each returns the positional op that gets signed.

TimeInForce = Literal["gtc", "ioc", "fok"]


@dataclass(frozen=True)
class Trigger:
    """A take-profit or stop-loss trigger. Without limit_price it executes at market."""

    kind: Literal["tp", "sl"]
    trigger_price: str
    limit_price: str | None = None


@dataclass(frozen=True)
class PerpsOrder:
    instrument_id: int
    buy: bool
    quantity: str
    price: str | None = None
    tif: TimeInForce | None = "gtc"
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str | None = None
    trigger: Trigger | None = None
    builder: tuple[str, str] | None = None


def order_row(order: PerpsOrder) -> list[Any]:
    trigger = None
    price = order.price
    if order.trigger is not None:
        trigger = [
            True if order.trigger.limit_price is None else None,
            order.trigger.trigger_price,
            order.trigger.kind,
        ]
        price = order.trigger.limit_price
    row: list[Any] = [
        order.instrument_id,
        order.buy,
        price,
        order.quantity,
        None if order.trigger is not None else order.tif,
        order.post_only,
        True if order.reduce_only else None,
        order.client_order_id,
        trigger,
    ]
    if order.builder is not None:
        row += [None, [order.builder[0], order.builder[1]]]
    return row


def create_orders(rows: Sequence[list[Any]], group: str | None = None) -> list[Any]:
    op: list[Any] = ["createOrders", list(rows)]
    if group is not None:
        op.append(group)
    return op


def cancel_orders(order_ids: Sequence[int]) -> list[Any]:
    return ["cancelOrders", list(order_ids)]


def cancel_orders_coid(client_order_ids: Sequence[str]) -> list[Any]:
    return ["cancelOrdersCOID", list(client_order_ids)]


def cancel_all(instrument_id: int | None = None) -> list[Any]:
    return ["cancelAll", [] if instrument_id is None else [instrument_id]]


def modify_orders(rows: Sequence[tuple[int, str, str]]) -> list[Any]:
    return ["modifyOrders", [list(row) for row in rows]]


def modify_orders_coid(rows: Sequence[tuple[str, str, str]]) -> list[Any]:
    return ["modifyOrdersCOID", [list(row) for row in rows]]


def auto_cancel(time_ms: int) -> list[Any]:
    return ["autoCancel", [time_ms]]


def update_leverage(instrument_id: int, leverage: int, cross: bool) -> list[Any]:
    return ["updateLeverage", [instrument_id, leverage, cross]]


def update_leverages(rows: Sequence[tuple[int, int, bool]]) -> list[Any]:
    return ["updateLeverages", [list(row) for row in rows]]


def update_margin(instrument_id: int, amount: str) -> list[Any]:
    return ["updateMargin", [instrument_id, amount]]


def delete_proxy(proxy: str) -> list[Any]:
    return ["deleteProxy", [proxy]]


def approve_builder(builder: str, max_fee_rate: str, version: int) -> list[Any]:
    return ["approveBuilder", [builder, max_fee_rate, version]]


def internal_transfer(account: str, token: str, amount: str, to: str) -> list[Any]:
    return ["internalTransfer", [account, token, amount, to]]


def _order_body(row: list[Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"iid": row[0], "buy": row[1], "po": row[5], "qty": row[3]}
    if row[4] is not None:
        body["tif"] = row[4]
    if row[6]:
        body["ro"] = row[6]
    if row[2] is not None:
        body["p"] = row[2]
    if row[7] is not None:
        body["c"] = row[7]
    if row[8] is not None:
        trigger = row[8]
        tr: dict[str, Any] = {"tpsl": trigger[2], "trp": trigger[1]}
        if trigger[0] is not None:
            tr["market"] = trigger[0]
        body["tr"] = tr
    if len(row) > 10 and row[10] is not None:
        body["builder"] = {"address": row[10][0], "fee_rate": row[10][1]}
    return body


def _op_cancel_list(args: Sequence[Any]) -> list[Any]:
    return list(args)


def _op_cancel_all(args: Sequence[Any]) -> dict[str, Any]:
    return {"iid": args[0]} if args else {}


def _op_auto_cancel(args: Sequence[Any]) -> dict[str, Any]:
    return {"time": args[0]}


def _op_update_leverage(args: Sequence[Any]) -> dict[str, Any]:
    return {"cross": args[2], "iid": args[0], "lev": args[1]}


def _op_update_leverages(args: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"cross": cross, "iid": iid, "lev": lev} for iid, lev, cross in args]


def _op_update_margin(args: Sequence[Any]) -> dict[str, Any]:
    return {"amt": args[1], "iid": args[0]}


def _op_modify_orders(args: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"oid": oid, "p": p, "qty": qty} for oid, p, qty in args]


def _op_modify_orders_coid(args: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"coid": coid, "p": p, "qty": qty} for coid, p, qty in args]


def _op_delete_proxy(args: Sequence[Any]) -> dict[str, Any]:
    return {"proxy": args[0]}


def _op_approve_builder(args: Sequence[Any]) -> dict[str, Any]:
    return {"builder": args[0], "max_fee_rate": args[1], "approval_version": args[2]}


def _op_internal_transfer(args: Sequence[Any]) -> dict[str, Any]:
    return {"account": args[0], "token": args[1], "amount": args[2], "to": args[3]}


_OP_ARG_BUILDERS: dict[str, Callable[[Sequence[Any]], Any]] = {
    "cancelOrders": _op_cancel_list,
    "cancelOrdersCOID": _op_cancel_list,
    "cancelAll": _op_cancel_all,
    "autoCancel": _op_auto_cancel,
    "updateLeverage": _op_update_leverage,
    "updateLeverages": _op_update_leverages,
    "updateMargin": _op_update_margin,
    "modifyOrders": _op_modify_orders,
    "modifyOrdersCOID": _op_modify_orders_coid,
    "deleteProxy": _op_delete_proxy,
    "approveBuilder": _op_approve_builder,
    "internalTransfer": _op_internal_transfer,
}


def body_op(op: Sequence[Any]) -> dict[str, Any]:
    """The JSON `op` of a request, built from the same positional op that is signed."""
    kind, args = op[0], op[1]
    if kind == "createOrders":
        body: dict[str, Any] = {"type": kind, "args": [_order_body(row) for row in args]}
        if len(op) > 2:
            body["grp"] = op[2]
        return body
    builder = _OP_ARG_BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"unsupported Perps command {kind!r}")
    return {"type": kind, "args": builder(args)}


def signed_request(
    private_key: str, op: Sequence[Any], *, salt: int, ts_ms: int, exp_ms: int | None = None
) -> dict[str, Any]:
    """A complete request body for a signed Perps endpoint."""
    body: dict[str, Any] = {
        "op": body_op(op),
        "sig": sign_op(private_key, op, salt=salt, ts_ms=ts_ms),
        "salt": salt,
        "ts": ts_ms,
    }
    if exp_ms is not None:
        body["exp"] = exp_ms
    return body
