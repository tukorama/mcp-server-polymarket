"""The Builder program: apps that route orders through Polymarket and earn fees."""

from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.paging import epoch_ms, epoch_seconds
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, WriteResult
from mcp_server_polymarket.models.data import DataRow, rows, slim
from mcp_server_polymarket.ops.perps_market import perps_slim
from mcp_server_polymarket.ops.registry import Call, Cursor, When, operation

TOOLSET = "builder"
_END = ("LTE=", "", None)


@operation("Get the builders leaderboard", toolset=TOOLSET, read_only=True)
async def get_builder_leaderboard(
    call: Call,
    view: Annotated[
        Literal["ranking", "volume_series"],
        Field(description="ranking: builders by volume; volume_series: volume per period."),
    ] = "ranking",
    window: Literal["day", "week", "month", "all"] = "day",
    limit: Annotated[int, Field(ge=1, le=90)] = 20,
    cursor: Cursor = None,
) -> CursorPage[DataRow]:
    """Builders (apps routing orders to Polymarket) ranked by volume and active users, or each
    builder's volume per day, week or month."""
    if view == "volume_series":
        body = await call.json(ep.DATA_BUILDER_VOLUME, params={"interval": window, "limit": limit})
    else:
        params: dict[str, Any] = {"time_period": window}
        params |= {"cursor": cursor} if cursor else {"limit": limit}
        body = await call.json(ep.DATA_BUILDER_LEADERBOARD, params=params)
    body = body if isinstance(body, dict) else {}
    pagination = body.get("pagination") or {}
    return CursorPage(
        items=rows(DataRow, body.get("data")), next_cursor=pagination.get("next_cursor")
    )


@operation("List a builder's trades", toolset=TOOLSET, read_only=True)
async def list_builder_trades(
    call: Call,
    builder_code: Annotated[
        str | None, Field(description="bytes32 builder code. Empty uses POLYMARKET_BUILDER_CODE.")
    ] = None,
    condition_id: str | None = None,
    token_id: str | None = None,
    after: When = None,
    before: When = None,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Trades attributed to a builder code, to verify attribution and fees."""
    code = builder_code or call.settings.builder_code
    if not code:
        raise InvalidRequest("Give builder_code, or set POLYMARKET_BUILDER_CODE.")
    params = {
        "builder_code": code,
        "market": condition_id,
        "asset_id": token_id,
        "after": epoch_seconds(after),
        "before": epoch_seconds(before),
        "next_cursor": cursor,
    }
    body = await call.json(ep.CLOB_BUILDER_TRADES, params={k: v for k, v in params.items() if v})
    body = body if isinstance(body, dict) else {"data": body}
    nxt = body.get("next_cursor")
    return CursorPage(
        items=[Raw.model_validate(slim(r)) for r in body.get("data") or [] if isinstance(r, dict)],
        next_cursor=None if nxt in _END else nxt,
    )


class KeyList(CompactModel):
    keys: list[Raw]


def _without_secrets(row: Any) -> Raw:
    row = dict(row) if isinstance(row, dict) else {"key": row}
    for secret in ("secret", "passphrase"):
        row.pop(secret, None)
    return Raw.model_validate(row)


@operation(
    "List my builder API keys",
    toolset=TOOLSET,
    requires=("clob_l2",),
    read_only=True,
)
async def list_builder_api_keys(call: Call) -> KeyList:
    """Builder API keys of the configured account. Secrets are never shown. Create or revoke
    them with `mcp-server-polymarket auth --builder-key`."""
    body = await call.json(ep.CLOB_BUILDER_KEYS)
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("keys") or [body]
    else:
        items = []
    return KeyList(keys=[_without_secrets(r) for r in items if r])


@operation(
    "List my relayer API keys",
    toolset=TOOLSET,
    requires=("relayer_key",),
    read_only=True,
)
async def list_relayer_api_keys(call: Call) -> KeyList:
    """Relayer API keys of the configured address. Secrets are never shown."""
    body = await call.json(ep.RELAYER_API_KEYS)
    items = body if isinstance(body, list) else (body or {}).get("keys") or []
    return KeyList(keys=[_without_secrets(r) for r in items])


class PerpsBuilder(CompactModel):
    approvals: list[Raw] | None = None
    earnings: list[Raw] | None = None
    summary: dict[str, Any] | None = None
    next_cursor: str | None = None


@operation(
    "Get my Perps builder earnings and approvals",
    toolset=TOOLSET,
    requires=("perps_session",),
    read_only=True,
)
async def get_my_perps_builder(
    call: Call,
    view: Literal["summary", "receipts", "approvals"] = "summary",
    builder: Annotated[str | None, Field(description="approvals: one builder's history.")] = None,
    start: Annotated[When, Field(description="Default seven days ago; at most 90 days.")] = None,
    end: When = None,
    cursor: Cursor = None,
) -> PerpsBuilder:
    """As a Perps builder: settled earnings summary or individual fee receipts. As a trader:
    the builder fee approvals you have granted."""
    if view == "approvals":
        body = await call.json(
            ep.PERPS_BUILDER_APPROVALS, params={"builder": builder} if builder else None
        )
        rows_ = body if isinstance(body, list) else (body or {}).get("approvals") or []
        return PerpsBuilder(approvals=[Raw.model_validate(perps_slim(r)) for r in rows_])
    window = {"start_timestamp": epoch_ms(start), "end_timestamp": epoch_ms(end)}
    params: dict[str, Any] = {k: v for k, v in window.items() if v}
    if view == "summary":
        body = await call.json(ep.PERPS_BUILDER_EARNINGS_SUMMARY, params=params or None)
        return PerpsBuilder(summary=perps_slim(body) if isinstance(body, dict) else None)
    if cursor:
        params["cursor"] = cursor
    body = await call.json(ep.PERPS_BUILDER_EARNINGS, params=params or None) or {}
    return PerpsBuilder(
        earnings=[Raw.model_validate(perps_slim(r)) for r in body.get("data") or []],
        next_cursor=body.get("cursor") or None,
    )


async def _describe_builder_fee(call: Call, max_fee_rate: str) -> str:
    builder = call.settings.perps_builder
    return (
        f"Let builder {builder} charge up to {max_fee_rate} (fraction of notional) on your Perps "
        "orders routed through it? Set 0 to revoke."
    )


@operation(
    "Approve a Perps builder fee",
    toolset=TOOLSET,
    requires=("perps_session", "perps_owner"),
    gate="trading",
    describe=_describe_builder_fee,
)
async def set_perps_builder_approval(
    call: Call,
    max_fee_rate: Annotated[
        str, Field(pattern=r"^0(\.\d+)?$", description="e.g. 0.0005; 0 revokes; at most 0.001.")
    ],
) -> WriteResult:
    """Approve, change or revoke the fee the builder configured in POLYMARKET_PERPS_BUILDER may
    charge on your Perps orders. Signed by the account owner."""
    import secrets
    import time
    from decimal import Decimal

    from mcp_server_polymarket import safety
    from mcp_server_polymarket.ops.perps_trading import owner_key
    from mcp_server_polymarket.signing.perps import approve_builder, signed_request

    builder = call.settings.perps_builder
    if not builder:
        raise InvalidRequest("Set POLYMARKET_PERPS_BUILDER to the builder's address first.")
    if Decimal(max_fee_rate) > Decimal("0.001"):
        raise InvalidRequest("Builder fees are capped at 0.001.")
    key, _ = owner_key(call)
    current = await call.json(ep.PERPS_BUILDER_APPROVALS, params={"builder": builder})
    rows = current if isinstance(current, list) else (current or {}).get("approvals") or []
    versions = [int(r.get("approval_version", 0)) for r in rows if isinstance(r, dict)]
    op = approve_builder(builder, max_fee_rate, max(versions, default=0) + 1)
    body = signed_request(key, op, salt=secrets.randbits(32), ts_ms=int(time.time() * 1000))
    data, unknown = await safety.send_write(
        call,
        tool="trading:set_perps_builder_approval",
        endpoint=ep.PERPS_APPROVE_BUILDER,
        body=body,
        summary=f"builder {builder} fee {max_fee_rate}",
        usd=None,
        print_=None,
    )
    return unknown or WriteResult(status="done", raw=perps_slim(data))
