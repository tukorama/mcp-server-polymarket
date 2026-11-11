"""Moving funds in from, and out to, other chains. Public API, no authentication."""

from typing import Annotated, Any

from pydantic import Field

from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.client.cache import HOUR
from mcp_server_polymarket.errors import InvalidRequest
from mcp_server_polymarket.models.base import CompactModel, CursorPage, Raw, WriteResult
from mcp_server_polymarket.ops.registry import Call, Cursor, operation

TOOLSET = "bridge"
Address = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]


def _headers(call: Call) -> dict[str, str] | None:
    code = call.settings.builder_code
    return {"X-Builder-Code": code} if code else None


class BridgeAsset(CompactModel):
    chain_id: str
    chain: str | None = None
    symbol: str | None = None
    name: str | None = None
    address: str | None = None
    decimals: int | None = None
    min_usd: float | None = None


class BridgeAssets(CompactModel):
    assets: list[BridgeAsset]
    note: str | None = None


@operation("List bridgeable assets", toolset=TOOLSET, read_only=True)
async def list_bridge_assets(
    call: Call,
    chain: Annotated[
        str | None, Field(description="Chain name or id, e.g. Ethereum or 8453.")
    ] = None,
    symbol: Annotated[str | None, Field(description="Token symbol, e.g. USDC.")] = None,
) -> BridgeAssets:
    """Chains and tokens that can be deposited to or withdrawn from Polymarket, with token
    addresses, decimals and minimum amounts. Cached for an hour."""
    cache = call.runtime.cache
    body = cache.get("bridge:assets", HOUR)
    if body is None:
        body = await call.json(ep.BRIDGE_ASSETS) or {}
        cache.set("bridge:assets", body)
    assets = []
    for row in body.get("supportedAssets") or []:
        token = row.get("token") or {}
        if chain and chain.lower() not in (
            str(row.get("chainId")),
            str(row.get("chainName", "")).lower(),
        ):
            continue
        if symbol and symbol.upper() != str(token.get("symbol", "")).upper():
            continue
        assets.append(
            BridgeAsset(
                chain_id=str(row.get("chainId")),
                chain=row.get("chainName"),
                symbol=token.get("symbol"),
                name=token.get("name"),
                address=token.get("address"),
                decimals=token.get("decimals"),
                min_usd=row.get("minCheckoutUsd"),
            )
        )
    return BridgeAssets(assets=assets, note=body.get("note"))


@operation("Get a bridge quote", toolset=TOOLSET, read_only=True)
async def get_bridge_quote(
    call: Call,
    amount_base_units: Annotated[
        str, Field(pattern=r"^\d+$", description="Amount in the source token's smallest unit.")
    ],
    from_chain_id: str,
    from_token_address: Address,
    to_chain_id: str,
    to_token_address: Address,
    recipient_address: Annotated[
        str | None, Field(description="Empty uses the configured wallet.")
    ] = None,
) -> Raw:
    """Estimated output, fees and time for a deposit or withdrawal through the bridge. Use
    list_bridge_assets for chain ids and token addresses. Nothing is moved."""
    recipient = recipient_address or call.identity.wallet_address
    if not recipient:
        raise InvalidRequest("Give recipient_address, or configure POLYMARKET_WALLET_ADDRESS.")
    body = await call.json(
        ep.BRIDGE_QUOTE,
        body={
            "fromAmountBaseUnit": amount_base_units,
            "fromChainId": from_chain_id,
            "fromTokenAddress": from_token_address,
            "recipientAddress": recipient,
            "toChainId": to_chain_id,
            "toTokenAddress": to_token_address,
        },
    )
    return Raw.model_validate(body if isinstance(body, dict) else {"quote": body})


@operation("Get bridge transfers", toolset=TOOLSET, read_only=True)
async def get_bridge_status(
    call: Call,
    address: Annotated[
        str, Field(description="A deposit or withdrawal address from the create_* tools.")
    ],
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    cursor: Cursor = None,
) -> CursorPage[Raw]:
    """Deposits and withdrawals seen at a bridge address, newest first, with their status
    (DEPOSIT_DETECTED, PROCESSING, SUBMITTED, COMPLETED, FAILED)."""
    params: dict[str, Any] = {"limit": limit, "paginate": "true"}
    if cursor:
        params["cursor"] = cursor
    body = await call.json(ep.BRIDGE_STATUS, path_args={"address": address}, params=params) or {}
    return CursorPage(
        items=[
            Raw.model_validate(r) for r in body.get("transactions") or [] if isinstance(r, dict)
        ],
        next_cursor=body.get("nextCursor"),
    )


class BridgeAddresses(WriteResult):
    status: str = "created"
    for_wallet: str
    addresses: dict[str, Any]
    note: str | None = None


@operation("Create bridge deposit addresses", toolset=TOOLSET, read_only=False, idempotent=True)
async def create_deposit_addresses(call: Call) -> BridgeAddresses:
    """Addresses on EVM chains, Solana, Bitcoin and Tron that credit the configured Polymarket
    wallet with pUSD when the user sends a supported asset to them. Calling it again returns
    the same addresses. Nothing is moved by this tool; check minimums with list_bridge_assets."""
    wallet = call.identity.wallet_address
    if not wallet:
        raise InvalidRequest(
            "No wallet is configured.", hint="Set POLYMARKET_WALLET_ADDRESS to your wallet."
        )
    body = await call.json(ep.BRIDGE_DEPOSIT, body={"address": wallet}, headers=_headers(call))
    body = body if isinstance(body, dict) else {}
    return BridgeAddresses(
        for_wallet=wallet, addresses=body.get("address") or {}, note=body.get("note")
    )


def _withdrawal_target(call: Call, to_chain_id: str, recipient: str | None) -> str:
    """Where bridged pUSD goes. Shared by the confirmation and the request.

    The Polymarket wallet is a Polygon contract (unless it is the key itself), so on any other
    chain the default is the owner's own key address, which exists on every EVM chain.
    """
    from mcp_server_polymarket import safety

    identity = call.identity
    owner = None if identity.is_session_key else identity.signer_address
    wallet = identity.verified_wallet
    eoa_wallet = wallet is not None and owner is not None and wallet.lower() == owner.lower()
    own_here = wallet if to_chain_id == "137" or eoa_wallet else None
    target = recipient or own_here or owner
    if not target:
        raise InvalidRequest(
            "Give a recipient from POLYMARKET_TRANSFER_ALLOWLIST, or configure the owner's key "
            "so the server can tell which addresses are yours."
        )
    safety.check_recipient(call, target, [own_here, owner])
    return target


async def _describe_withdrawal(
    call: Call, to_chain_id: str, to_token_address: str, recipient: str | None = None
) -> str:
    target = _withdrawal_target(call, to_chain_id, recipient)
    return (
        f"Create bridge withdrawal addresses that send to {target} on chain {to_chain_id} "
        f"(token {to_token_address})? Any pUSD sent to them later leaves Polymarket."
    )


@operation(
    "Create bridge withdrawal addresses",
    toolset=TOOLSET,
    gate="transfers",
    describe=_describe_withdrawal,
)
async def create_withdrawal_addresses(
    call: Call,
    to_chain_id: Annotated[str, Field(description="Destination chain id, e.g. 8453 for Base.")],
    to_token_address: Address,
    recipient: Annotated[
        str | None,
        Field(description="Who receives it. Empty: your wallet. Others must be allowlisted."),
    ] = None,
) -> BridgeAddresses:
    """Addresses that bridge pUSD out of Polymarket to a token on another chain. Funds move only
    when pUSD is sent to them, which this tool does not do."""
    wallet = call.identity.wallet_address
    if not wallet:
        raise InvalidRequest("No wallet is configured.", hint="Set POLYMARKET_WALLET_ADDRESS.")
    target = _withdrawal_target(call, to_chain_id, recipient)
    body = await call.json(
        ep.BRIDGE_WITHDRAW,
        body={
            "address": wallet,
            "toChainId": to_chain_id,
            "toTokenAddress": to_token_address,
            "recipientAddr": target,
        },
        headers=_headers(call),
    )
    body = body if isinstance(body, dict) else {}
    return BridgeAddresses(
        for_wallet=wallet, addresses=body.get("address") or {}, note=body.get("note")
    )
