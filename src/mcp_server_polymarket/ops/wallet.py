"""Deposit Wallet transactions through the Relayer: approvals, split, merge and redeem.

The calls and their targets are fixed by this code from the market's own data. Tools take a
market and an amount, never an address or calldata.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from mcp_server_polymarket import safety, wallet_tx
from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.errors import InvalidRequest, NotFound, Refused
from mcp_server_polymarket.models.base import WriteResult, json_list
from mcp_server_polymarket.ops.registry import Call, ConditionId, operation
from mcp_server_polymarket.redaction import clean_label

TOOLSET = "wallet"
OWNER = ("owner_signer", "relayer_key")
DryRun = Annotated[bool, Field(description="Show the transaction without sending it.")]


class WalletResult(WriteResult):
    transaction_id: str | None = None
    state: str | None = None
    transaction_hash: str | None = None
    calls: list[str] | None = None


@dataclass
class Plan:
    calls: list[Any]
    lines: list[str]
    usd: Decimal | None
    metadata: str


@dataclass
class MarketContext:
    condition_id: str
    question: str | None
    v2: bool
    neg_risk: bool
    tokens: list[str]


async def market_context(call: Call, condition_id: str) -> MarketContext:
    body = await call.json(
        ep.GAMMA_MARKETS_KEYSET,
        params={"condition_ids": condition_id, "closed": "any", "limit": 1},
    )
    rows = (body or {}).get("markets") or [] if isinstance(body, dict) else []
    if not rows:
        raise NotFound(f"No market with condition id {condition_id}.")
    market = rows[0]
    v2 = market.get("version") == "v2"
    tokens = json_list(market.get("positionIds")) if v2 else json_list(market.get("clobTokenIds"))
    return MarketContext(
        condition_id=condition_id,
        question=clean_label(market.get("question")),
        v2=v2,
        neg_risk=bool(market.get("negRisk")),
        tokens=[str(t) for t in tokens],
    )


def _targets(market: MarketContext) -> tuple[str, str]:
    """(position contract to call, ERC-1155 that holds the outcome tokens)."""
    from mcp_server_polymarket.signing import wallets as w

    if market.v2:
        return w.ROUTER_V2, w.POSITION_MANAGER
    if market.neg_risk:
        return w.NEG_RISK_COLLATERAL_ADAPTER, w.NEG_RISK_ADAPTER
    return w.COLLATERAL_ADAPTER, w.CONDITIONAL_TOKENS


def _units(amount: Decimal) -> int:
    units = amount * 1_000_000
    if units != units.to_integral_value() or units <= 0:
        raise InvalidRequest("Amounts have at most 6 decimals and must be positive.")
    return int(units)


def _recheck(call: Call, plan: Plan, print_: str) -> None:
    if plan.usd is not None:
        safety.check_daily(call, plan.usd, "trading")
    safety.block_duplicate(call, print_)


async def _send(call: Call, plan: Plan, tool: str, dry_run: bool) -> WalletResult:
    if dry_run:
        return WalletResult(status="preview", dry_run=True, calls=plan.lines)
    print_ = safety.fingerprint(tool, [(c.target, c.data) for c in plan.calls])
    if plan.usd is not None:
        safety.check_limit(
            call.settings.max_order_usd, plan.usd, "This transaction", "POLYMARKET_MAX_ORDER_USD"
        )
        safety.check_daily(call, plan.usd, "trading")
    safety.block_duplicate(call, print_)
    tx = await wallet_tx.submit(
        call.client,
        call.identity,
        plan.calls,
        metadata=plan.metadata,
        wait_seconds=call.limit_wait(30),
        deadline=call.deadline,
        record=wallet_tx.Record(
            journal=call.runtime.journal,
            tool=tool,
            summary="; ".join(plan.lines),
            usd=plan.usd,
            fingerprint=print_,
            check=lambda: _recheck(call, plan, print_),
        ),
    )
    if tx.state == "UNKNOWN":
        return WalletResult(
            **safety.unknown_result(tx.request_id or "", tx.error or "no answer").model_dump(),
            calls=plan.lines,
        )
    status = {
        "STATE_CONFIRMED": "confirmed",
        "STATE_FAILED": "failed",
        "STATE_INVALID": "failed",
    }.get(tx.state or "", "pending")
    return WalletResult(
        status=status,
        transaction_id=tx.transaction_id,
        state=tx.state,
        transaction_hash=tx.transaction_hash,
        message=tx.error,
        calls=plan.lines,
        next_step="Follow it with get_relayer_transaction." if status == "pending" else None,
    )


# Approvals


def approval_plan(existing: Any) -> Plan:
    from mcp_server_polymarket.signing import relayer
    from mcp_server_polymarket.signing import wallets as w

    wanted = [
        ("erc20", w.PUSD, w.EXCHANGE_V3, "pUSD for the V2 exchange"),
        ("erc20", w.PUSD, w.STANDARD_EXCHANGE, "pUSD for the standard exchange"),
        ("erc20", w.PUSD, w.NEG_RISK_EXCHANGE, "pUSD for the neg-risk exchange"),
        ("erc20", w.PUSD, w.NEG_RISK_ADAPTER, "pUSD for the neg-risk adapter"),
        ("erc1155", w.POSITION_MANAGER, w.EXCHANGE_V3, "V2 outcome tokens for the V2 exchange"),
        (
            "erc1155",
            w.CONDITIONAL_TOKENS,
            w.STANDARD_EXCHANGE,
            "outcome tokens for the standard exchange",
        ),
        (
            "erc1155",
            w.CONDITIONAL_TOKENS,
            w.NEG_RISK_EXCHANGE,
            "outcome tokens for the neg-risk exchange",
        ),
        (
            "erc1155",
            w.CONDITIONAL_TOKENS,
            w.NEG_RISK_ADAPTER,
            "outcome tokens for the neg-risk adapter",
        ),
    ]
    approved = set()
    for contract in (existing or {}).get("contracts") or []:
        if contract.get("approved"):
            approved.add(
                (str(contract.get("token", "")).lower(), str(contract.get("spender", "")).lower())
            )
    calls, lines = [], []
    for kind, token, spender, label in wanted:
        if (token.lower(), spender.lower()) in approved:
            continue
        if kind == "erc20":
            calls.append(relayer.erc20_approve(token, spender, relayer.MAX_UINT256))
        else:
            calls.append(relayer.erc1155_set_approval_for_all(token, spender))
        lines.append(f"approve {label} ({spender})")
    return Plan(calls=calls, lines=lines, usd=None, metadata="Trading approvals")


async def _describe_approvals(call: Call, dry_run: bool = False) -> str:
    existing = await call.json(ep.DATA_APPROVALS, params={"user": call.identity.wallet_address})
    plan = approval_plan((existing or {}).get("data"))
    if not plan.lines:
        return "All trading approvals are already in place. Continue anyway (nothing will be sent)?"
    return (
        f"Grant these approvals from {call.identity.wallet_address}, so Polymarket's exchanges "
        "can settle your orders:\n- " + "\n- ".join(plan.lines) + "\nSend this transaction?"
    )


@operation(
    "Set up trading approvals",
    toolset=TOOLSET,
    requires=OWNER,
    gate="trading",
    describe=_describe_approvals,
)
async def setup_trading_approvals(call: Call, dry_run: DryRun = False) -> WalletResult:
    """Approve Polymarket's exchanges and the neg-risk adapter to move the Deposit Wallet's pUSD
    and outcome tokens, which orders need to settle. Only missing approvals are sent, in one
    gasless transaction. The spenders are fixed Polymarket contracts."""
    existing = await call.json(ep.DATA_APPROVALS, params={"user": call.identity.wallet_address})
    plan = approval_plan((existing or {}).get("data"))
    if not plan.calls:
        return WalletResult(status="nothing_to_do", message="Every approval is already in place.")
    return await _send(call, plan, "trading:setup_trading_approvals", dry_run)


# Split, merge, redeem


def split_plan(market: MarketContext, units: int) -> Plan:
    from mcp_server_polymarket.signing import relayer
    from mcp_server_polymarket.signing import wallets as w

    target, _ = _targets(market)
    split = (
        relayer.router_split(target, market.condition_id, units)
        if market.v2
        else relayer.ctf_split(target, market.condition_id, units)
    )
    amount = Decimal(units) / 1_000_000
    return Plan(
        # An exact approval in the same batch: nothing is left approved afterwards.
        calls=[relayer.erc20_approve(w.PUSD, target, units), split],
        lines=[f"approve {amount} pUSD for {target}", f"split {amount} pUSD into Yes and No"],
        usd=amount,
        metadata="Split position",
    )


async def _describe_split(
    call: Call, condition_id: str, amount: Decimal, dry_run: bool = False
) -> str:
    market = await market_context(call, condition_id)
    return (
        f"Split {amount} pUSD into {amount} Yes and {amount} No shares of '{market.question}' "
        f"in wallet {call.identity.wallet_address}?"
    )


@operation(
    "Split pUSD into outcome shares",
    toolset=TOOLSET,
    requires=OWNER,
    gate="trading",
    describe=_describe_split,
)
async def split_position(
    call: Call,
    condition_id: ConditionId,
    amount: Annotated[Decimal, Field(gt=0, description="pUSD to split, e.g. 10.")],
    dry_run: DryRun = False,
) -> WalletResult:
    """Turn pUSD into an equal number of Yes and No shares of a market (1 pUSD makes one of
    each). Useful before quoting both sides."""
    market = await market_context(call, condition_id)
    plan = split_plan(market, _units(amount))
    return await _send(call, plan, "trading:split_position", dry_run)


async def _held(call: Call, condition_id: str, status: str) -> list[dict[str, Any]]:
    body = await call.json(
        ep.DATA_POSITIONS,
        params={
            "user": call.identity.wallet_address,
            "condition": condition_id,
            "status": status,
            "limit": 10,
        },
    )
    rows = (body or {}).get("data") or [] if isinstance(body, dict) else []
    return [r for r in rows if isinstance(r, dict)]


def merge_plan(market: MarketContext, units: int) -> Plan:
    from mcp_server_polymarket.signing import relayer

    target, tokens = _targets(market)
    merge = (
        relayer.router_merge(target, market.condition_id, units)
        if market.v2
        else relayer.ctf_merge(target, market.condition_id, units)
    )
    amount = Decimal(units) / 1_000_000
    return Plan(
        # The approval is withdrawn in the same batch, so nothing stays approved afterwards.
        calls=[
            relayer.erc1155_set_approval_for_all(tokens, target),
            merge,
            relayer.erc1155_set_approval_for_all(tokens, target, False),
        ],
        lines=[
            f"approve outcome tokens for {target}",
            f"merge {amount} Yes + No into {amount} pUSD",
            f"withdraw the approval for {target}",
        ],
        usd=None,
        metadata="Merge positions",
    )


async def _describe_merge(
    call: Call, condition_id: str, shares: Decimal | None = None, dry_run: bool = False
) -> str:
    market = await market_context(call, condition_id)
    how_many = f"{shares} pairs" if shares is not None else "all complete pairs"
    return (
        f"Merge {how_many} of Yes and No shares of '{market.question}' back into pUSD in "
        f"wallet {call.identity.wallet_address}?"
    )


@operation(
    "Merge outcome shares into pUSD",
    toolset=TOOLSET,
    requires=OWNER,
    gate="trading",
    describe=_describe_merge,
)
async def merge_positions(
    call: Call,
    condition_id: ConditionId,
    shares: Annotated[
        Decimal | None, Field(gt=0, description="Pairs to merge. Empty merges all you can.")
    ] = None,
    dry_run: DryRun = False,
) -> WalletResult:
    """Turn equal numbers of Yes and No shares back into pUSD (one pair returns 1 pUSD)."""
    market = await market_context(call, condition_id)
    if shares is None:
        rows = await _held(call, condition_id, "MERGEABLE")
        sizes = [Decimal(str(r.get("current_size") or 0)) for r in rows]
        if len(sizes) < 2 or min(sizes) <= 0:
            raise Refused("There are no complete Yes and No pairs to merge in this market.")
        shares = min(sizes).quantize(Decimal("0.000001"), rounding="ROUND_FLOOR")
    plan = merge_plan(market, _units(shares))
    return await _send(call, plan, "trading:merge_positions", dry_run)


async def redeem_plan(call: Call, market: MarketContext) -> Plan:
    from mcp_server_polymarket.signing import relayer

    target, tokens = _targets(market)
    calls = [relayer.erc1155_set_approval_for_all(tokens, target)]
    lines = [f"approve outcome tokens for {target}"]
    if market.v2:
        rows = await _held(call, market.condition_id, "REDEEMABLE")
        for row in rows:
            index = row.get("outcome_index")
            size = Decimal(str(row.get("current_size") or 0))
            if index in (0, 1) and size > 0:
                units = _units(size.quantize(Decimal("0.000001"), rounding="ROUND_FLOOR"))
                calls.append(relayer.router_redeem(target, market.condition_id, index, units))
                lines.append(f"redeem {size} shares of outcome {index}")
        if len(calls) == 1:
            raise Refused("Nothing in this market is redeemable for the wallet.")
    else:
        calls.append(relayer.ctf_redeem(target, market.condition_id))
        lines.append("redeem every resolved share of this market")
    calls.append(relayer.erc1155_set_approval_for_all(tokens, target, False))
    lines.append(f"withdraw the approval for {target}")
    return Plan(calls=calls, lines=lines, usd=None, metadata="Redeem positions")


async def _describe_redeem(call: Call, condition_id: str, dry_run: bool = False) -> str:
    market = await market_context(call, condition_id)
    return (
        f"Redeem the resolved shares of '{market.question}' for pUSD in wallet "
        f"{call.identity.wallet_address}?"
    )


@operation(
    "Redeem resolved shares",
    toolset=TOOLSET,
    requires=OWNER,
    gate="trading",
    describe=_describe_redeem,
)
async def redeem_positions(
    call: Call, condition_id: ConditionId, dry_run: DryRun = False
) -> WalletResult:
    """Claim pUSD for winning shares of a resolved market. Find them with list_positions
    (status REDEEMABLE)."""
    market = await market_context(call, condition_id)
    plan = await redeem_plan(call, market)
    return await _send(call, plan, "trading:redeem_positions", dry_run)
