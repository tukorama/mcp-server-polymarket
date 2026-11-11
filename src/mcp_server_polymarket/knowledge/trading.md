# Trading prediction markets

Trading tools exist only when the user enabled them (`POLYMARKET_ALLOW_TRADING=1`) and configured
a signing key and wallet. Place an order only when the user asked for that specific order in this
conversation.

## Before an order

1. Find the market and its `token_id` with `search` / `get_market`.
2. `get_quotes` for best bid and ask, tick size, minimum size and whether the market is neg-risk.
3. `get_my_balance_allowance` to check pUSD (for BUY) or shares (for SELL). If the allowance is
   missing, `setup_trading_approvals` sets it once per wallet.
4. Call the order tool with `dry_run=true` to see the exact amounts without sending anything.

## Order tools

| Tool | Use |
| --- | --- |
| `place_limit_order` | Rest at a price until filled, cancelled or `expires_at`. `post_only` never takes. |
| `place_market_order` | Trade now. BUY `amount` is pUSD to spend, SELL `amount` is shares. `price_bound` is required: the worst price you accept. `FAK` fills what it can, `FOK` all or nothing. |
| `place_orders` | Up to 15 limit orders in one request; each succeeds or fails on its own. |
| `cancel_orders` / `cancel_all_orders` | Cancel by id, by market or token, or everything. Safe to repeat. |

Prices must be on the market's tick: an off-tick price is refused, never rounded. A price far
from the book (more than `POLYMARKET_MAX_PRICE_DEVIATION`, 10% by default) is refused as a likely
mistake.

## Results

- `placed`, with `order_id` and `order_status` (`live`, `matched`, `delayed`).
- `rejected`, with Polymarket's reason. "not enough balance / allowance" means funds or approvals
  are missing: tell the user, do not retry.
- `declined`: the user said no. Do not try again unless they ask.
- `unknown`: the connection failed after sending, so the order may or may not exist. Do not place
  it again. Check `list_my_open_orders` / `list_my_trades`, then use `list_unconfirmed_writes`
  and `resend_write`, which sends the identical signed bytes (Polymarket rejects a duplicate).

## Positions on-chain

`wallet` tools send gasless Deposit Wallet transactions through the Relayer:

- `split_position`: pUSD into an equal number of Yes and No shares.
- `merge_positions`: equal Yes and No shares back into pUSD.
- `redeem_positions`: winnings of a resolved market into pUSD.

They wait for confirmation up to a time limit and otherwise return `pending` with an id for
`get_relayer_transaction`.

## Fees and rewards

Fees depend on the market (`get_market_trading_info`). Resting orders near the midpoint can earn
liquidity rewards; see the `rewards` toolset.
