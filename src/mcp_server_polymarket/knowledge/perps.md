# Perps

Perpetual futures margined in pUSD. Every tool takes an integer `instrument_id` from
`list_perps_instruments`, which also gives each instrument's price and quantity decimals,
maximum leverage, minimum notional and funding interval.

## Market data

- `get_perps_tickers`: mark, index and last price, 24 h change and volume, funding rate, open
  interest, best bid and ask.
- `get_perps_book`, `list_perps_trades`, `get_perps_candles` (`klines` or `mark` history).
- `get_perps_funding_history`: funding is paid between longs and shorts every interval; a
  positive rate means longs pay shorts.
- `get_perps_exchange_info`: assets, fee tiers and limit tiers.

## Account

Reading the account needs a Perps session, created once in the terminal with
`mcp-server-polymarket auth --perps`. That registers a separate trading key (the proxy) which
can place orders but cannot withdraw. `get_my_perps_portfolio` shows equity, available margin,
positions with entry, mark, liquidation price and unrealised PnL.

## Orders

`place_perps_orders` takes a list of orders:

- `side` BUY (long) or SELL (short), `quantity` in the instrument's base unit, `price`.
- `time_in_force`: `gtc` rests on the book; `ioc` and `fok` trade now, and may omit `price` to
  trade at market.
- `post_only`, `reduce_only` (only shrinks a position; exempt from the USD limit).
- `take_profit` / `stop_loss` with a `trigger_price` and optional `limit_price` attach reduce-only exit
  orders that trigger at that price.

Quantities and prices must respect the instrument's decimals; they are refused, not rounded.
Notional is `quantity × max(price, mark)` and is checked against `POLYMARKET_MAX_PERPS_ORDER_USD`.

Other tools: `cancel_perps_orders`, `cancel_all_perps_orders`, `modify_perps_orders`
(experimental), `set_perps_leverage` (capped by `POLYMARKET_MAX_PERPS_LEVERAGE`, cross or
isolated), `adjust_perps_margin` (isolated positions), `set_perps_auto_cancel` (a dead man's
switch: all orders are cancelled if not renewed in time; 0 turns it off).

## Funds

- `deposit_to_perps` moves pUSD from the Deposit Wallet to the Perps account (minimum 10 pUSD).
- `withdraw_from_perps` sends pUSD back, by default to the user's own wallet. It is signed by the
  owner key, never by the trading key.

Both need `POLYMARKET_ALLOW_TRANSFERS=1` and the user's confirmation.

## Risk

Leverage multiplies gains and losses. A position is liquidated when its margin falls below the
maintenance requirement. Before proposing a trade, state the entry, size, leverage, liquidation
price and the loss at the stop.
