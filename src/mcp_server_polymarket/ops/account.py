"""The configured account's CLOB orders, trades, balances and Relayer transactions."""

import asyncio
import time
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.base import describe_error
from mcp_server_polymarket.client.paging import epoch_seconds
from mcp_server_polymarket.errors import InvalidRequest, NotFound
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, Section
from mcp_server_polymarket.models.data import slim
from mcp_server_polymarket.ops.registry import Call, Cursor, TokenId, When, operation
from mcp_server_polymarket.protocol import is_v2_token

TOOLSET = "account"
L2 = ("clob_l2",)
_END = ("LTE=", "", None)


def _page(body: Any) -> CursorPage[Raw]:
    body = body if isinstance(body, dict) else {"data": body}
    rows = body.get("data")
    rows = rows if isinstance(rows, list) else []
    cursor = body.get("next_cursor")
    return CursorPage(
        items=[Raw.model_validate(slim(r)) for r in rows if isinstance(r, dict)],
        next_cursor=None if cursor in _END else cursor,
        count=body.get("count"),
    )


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v not in (None, "")}


class AccountStatus(CompactModel):
    signer: str | None = None
    wallet: str | None = None
    signature_type: int | None = None
    closed_only: bool | None = None
    rate_limit_tier: dict[str, Any] | None = None
    api_keys: list[Any] | None = None
    session_keys: list[Any] | None = None
    errors: list[Section] | None = None


@operation("Get my CLOB account status", toolset=TOOLSET, requires=L2, read_only=True)
async def get_my_account_status(call: Call) -> AccountStatus:
    """Whether the account is in closed-only mode, its trading rate-limit tier, its API keys
    (with a signing key configured) and, for a Deposit Wallet, its session keys."""
    identity = call.identity
    names = ["closed_only", "rate_limit_tier"]
    calls = [call.json(ep.CLOB_CLOSED_ONLY), call.json(ep.CLOB_RATE_LIMIT_TIER)]
    if identity.signature_type == 3:
        names.append("session_keys")
        calls.append(call.json(ep.CLOB_SESSION_SIGNERS))
    if identity.signer_key:
        from mcp_server_polymarket.signing.clob import l1_headers

        names.append("api_keys")
        calls.append(
            call.json(ep.CLOB_API_KEYS, headers=l1_headers(identity.signer_key, int(time.time())))
        )
    results = dict(zip(names, await asyncio.gather(*calls, return_exceptions=True), strict=True))
    errors = [
        Section(section=name, error=describe_error(value))
        for name, value in results.items()
        if isinstance(value, BaseException)
    ]

    def ok(name: str) -> Any:
        value = results.get(name)
        return None if isinstance(value, BaseException) else value

    signers = ok("session_keys")
    keys = ok("api_keys")
    return AccountStatus(
        signer=identity.clob_address,
        wallet=identity.wallet_address,
        signature_type=identity.signature_type,
        closed_only=(ok("closed_only") or {}).get("closed_only"),
        rate_limit_tier=ok("rate_limit_tier"),
        api_keys=(keys or {}).get("apiKeys") if isinstance(keys, dict) else None,
        session_keys=(signers or {}).get("signers") if isinstance(signers, dict) else None,
        errors=errors or None,
    )


@operation("List my open orders", toolset=TOOLSET, requires=L2, read_only=True)
async def list_my_open_orders(
    call: Call,
    condition_id: Annotated[str | None, Field(description="Only this market.")] = None,
    token_id: Annotated[str | None, Field(description="Only this outcome token.")] = None,
    whole_wallet: Annotated[
        bool,
        Field(
            description="Deposit Wallet: orders from every key and session key, not just "
            "this API key."
        ),
    ] = False,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Your resting orders: price, original size, size matched, side, outcome and expiration.
    By default only orders placed with the configured API key."""
    if whole_wallet:
        body = await call.json(
            ep.CLOB_WALLET_OPEN_ORDERS,
            params=_clean({"market": condition_id, "asset_id": token_id, "cursor": cursor}),
        )
    else:
        body = await call.json(
            ep.CLOB_ORDERS,
            params=_clean({"market": condition_id, "asset_id": token_id, "next_cursor": cursor}),
        )
    return _page(body)


@operation("Get one of my orders", toolset=TOOLSET, requires=L2, read_only=True)
async def get_my_order(
    call: Call, order_id: Annotated[str, Field(description="Order hash.")]
) -> Raw:
    """One order by id, including cancelled and fully matched ones, with its trades."""
    body = await call.json(ep.CLOB_ORDER, path_args={"order_id": order_id})
    if not body:
        raise NotFound(f"No order {order_id} on this account.")
    return Raw.model_validate(slim(body))


@operation("List my CLOB trades", toolset=TOOLSET, requires=L2, read_only=True)
async def list_my_trades(
    call: Call,
    condition_id: str | None = None,
    token_id: str | None = None,
    trade_id: str | None = None,
    after: When = None,
    before: When = None,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Your fills on the CLOB with status (MATCHED, MINED, CONFIRMED, FAILED), fee and the
    maker orders matched. For the on-chain view use list_trades with your wallet."""
    maker = call.identity.wallet_address or call.identity.clob_address
    body = await call.json(
        ep.CLOB_TRADES,
        params=_clean(
            {
                "maker_address": maker,
                "market": condition_id,
                "asset_id": token_id,
                "id": trade_id,
                "after": epoch_seconds(after),
                "before": epoch_seconds(before),
                "next_cursor": cursor,
            }
        ),
    )
    return _page(body)


class BalanceAllowance(CompactModel):
    asset: str
    balance: str | None = None
    balance_display: str | None = None
    allowances: dict[str, Any] | None = None
    refreshed: bool | None = None


@operation("Get my balance and allowances", toolset=TOOLSET, requires=L2, read_only=True)
async def get_my_balance_allowance(
    call: Call,
    token_id: Annotated[
        TokenId | None, Field(description="An outcome token; empty means pUSD collateral.")
    ] = None,
    refresh: Annotated[
        bool, Field(description="Re-read balances and allowances from the chain first.")
    ] = False,
) -> BalanceAllowance:
    """pUSD or outcome-token balance the CLOB sees for the wallet, and the exchange
    allowances. Refresh after approvals, deposits, or before a first sell."""
    if token_id is None:
        params: dict[str, Any] = {"asset_type": "COLLATERAL"}
    else:
        kind = "CONDITIONAL-V2" if is_v2_token(token_id) else "CONDITIONAL"
        params = {"asset_type": kind, "token_id": token_id}
    if call.identity.signature_type is not None:
        params["signature_type"] = call.identity.signature_type
    if refresh:
        await call.json(ep.CLOB_BALANCE_ALLOWANCE_UPDATE, params=params)
    body = await call.json(ep.CLOB_BALANCE_ALLOWANCE, params=params) or {}
    balance = body.get("balance")
    display = None
    if balance is not None and str(balance).isdigit():
        display = format((Decimal(int(balance)) / 1_000_000).normalize(), "f")
    return BalanceAllowance(
        asset=token_id or "pUSD",
        balance=None if balance is None else str(balance),
        balance_display=display,
        allowances=body.get("allowances"),
        refreshed=refresh or None,
    )


@operation("List my CLOB notifications", toolset=TOOLSET, requires=L2, read_only=True)
async def list_my_notifications(call: Call) -> CursorPage[Raw]:
    """Unread notifications: order fills and cancellations, resolved markets. Mark them read
    with mark_notifications_read."""
    signature_type = call.identity.signature_type
    params = {"signature_type": signature_type if signature_type is not None else 0}
    body = await call.json(ep.CLOB_NOTIFICATIONS, params=params)
    rows = body if isinstance(body, list) else (body or {}).get("data") or []
    return CursorPage(items=[Raw.model_validate(slim(r)) for r in rows if isinstance(r, dict)])


class WalletStatus(CompactModel):
    wallet: str | None = None
    signer: str | None = None
    kind: str | None = None
    deployed: bool | None = None
    nonce: str | None = None
    relayer: dict[str, Any] | None = None
    errors: list[Section] | None = None


_KIND_NAMES = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE", 3: "DEPOSIT_WALLET"}


@operation("Get my wallet's relayer status", toolset=TOOLSET, requires=L2, read_only=True)
async def get_my_wallet_status(call: Call) -> WalletStatus:
    """The configured wallet's kind, whether it is deployed on chain, and its Relayer nonce.
    Gasless transactions need a deployed wallet."""
    identity = call.identity
    signer = identity.signer_address or identity.clob_address
    wallet = identity.wallet_address
    kind = identity.signature_type
    if not signer:
        raise InvalidRequest("No signer is configured.")
    names, calls = [], []
    if kind == 3 and wallet:
        names.append("deployed")
        calls.append(call.json(ep.RELAYER_DEPLOYED, params={"address": wallet, "type": "WALLET"}))
        if identity.relayer_key() or identity.builder_key():
            names.append("nonce")
            calls.append(
                call.json(ep.RELAYER_TX_PARAMS, params={"address": signer, "type": "WALLET"})
            )
    elif kind in (1, 2):
        relay_type = "PROXY" if kind == 1 else "SAFE"
        if kind == 2:
            names.append("deployed")
            calls.append(call.json(ep.RELAYER_DEPLOYED, params={"address": signer, "type": "SAFE"}))
        names += ["nonce", "relayer"]
        calls += [
            call.json(ep.RELAYER_NONCE, params={"address": signer, "type": relay_type}),
            call.json(ep.RELAYER_RELAY_PAYLOAD, params={"address": signer, "type": relay_type}),
        ]
    results = dict(zip(names, await asyncio.gather(*calls, return_exceptions=True), strict=True))

    def ok(name: str) -> dict[str, Any]:
        value = results.get(name)
        return value if isinstance(value, dict) else {}

    return WalletStatus(
        wallet=wallet,
        signer=signer,
        kind=_KIND_NAMES.get(kind) if kind is not None else None,
        deployed=ok("deployed").get("deployed"),
        nonce=str(ok("nonce")["nonce"]) if "nonce" in ok("nonce") else None,
        relayer=ok("relayer") or None,
        errors=[
            Section(section=n, error=describe_error(v))
            for n, v in results.items()
            if isinstance(v, BaseException)
        ]
        or None,
    )


@operation("Get a relayer transaction", toolset=TOOLSET, requires=L2, read_only=True)
async def get_relayer_transaction(
    call: Call,
    transaction_id: str,
    kind: Annotated[
        Literal["wallet", "legacy"],
        Field(description="wallet: Deposit Wallet batches; legacy: Safe and proxy transactions."),
    ] = "wallet",
) -> Raw:
    """A gasless transaction's state (STATE_NEW ... STATE_CONFIRMED, or STATE_FAILED and
    STATE_INVALID) and its on-chain hash."""
    if kind == "wallet":
        body = await call.json(ep.RELAYER_TX, path_args={"id": transaction_id})
    else:
        body = await call.json(ep.RELAYER_TRANSACTION, params={"id": transaction_id})
        body = body[0] if isinstance(body, list) and body else body
    return Raw.model_validate(slim(body) if isinstance(body, dict) else {"transaction": body})


@operation(
    "List my relayer transactions",
    toolset=TOOLSET,
    requires=("clob_l2", "relayer_key"),
    read_only=True,
)
async def list_relayer_transactions(call: Call) -> CursorPage[Raw]:
    """Recent gasless transactions of the Relayer key's address, newest first."""
    body = await call.json(ep.RELAYER_TRANSACTIONS)
    rows = body if isinstance(body, list) else []
    return CursorPage(items=[Raw.model_validate(slim(r)) for r in rows if isinstance(r, dict)])
