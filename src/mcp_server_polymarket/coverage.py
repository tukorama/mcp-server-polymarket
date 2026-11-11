"""How every published endpoint is covered when no tool calls it directly.

Keys are `SERVICE METHOD /path`, with path placeholders written as `{}`. `tests/test_coverage.py`
checks that the published specifications, the endpoints in `client/endpoints.py` and these
tables account for each other exactly.
"""

# Served by another endpoint that returns the same data, so no tool needs this one.
SUPERSEDED = {
    "CLOB GET /books": "POST /books returns the same books for any number of tokens",
    "CLOB GET /price": "get_quotes reads best bid and ask from POST /books",
    "CLOB GET /prices": "get_quotes reads best bid and ask from POST /books",
    "CLOB POST /prices": "get_quotes reads best bid and ask from POST /books",
    "CLOB GET /midpoint": "get_quotes computes the midpoint from POST /books",
    "CLOB GET /midpoints": "get_quotes computes the midpoint from POST /books",
    "CLOB POST /midpoints": "get_quotes computes the midpoint from POST /books",
    "CLOB GET /spread": "get_quotes computes the spread from POST /books",
    "CLOB POST /spreads": "get_quotes computes the spread from POST /books",
    "CLOB GET /last-trade-price": "POST /books carries last_trade_price",
    "CLOB GET /last-trades-prices": "POST /books carries last_trade_price",
    "CLOB POST /last-trades-prices": "POST /books carries last_trade_price",
    "CLOB GET /tick-size": "/book and /clob-markets carry the tick size",
    "CLOB GET /tick-size/{}": "/book and /clob-markets carry the tick size",
    "CLOB GET /neg-risk": "/book and /clob-markets carry the neg-risk flag",
    "CLOB GET /neg-risk/{}": "/book and /clob-markets carry the neg-risk flag",
    "CLOB GET /fee-rate": "/clob-markets carries base fees and the fee curve",
    "CLOB GET /fee-rate/{}": "/clob-markets carries base fees and the fee curve",
    "CLOB GET /prices-history": "the Data API's /v2/prices-history replaces it",
    "CLOB POST /batch-prices-history": "the Data API's /v2/prices-history replaces it",
    "CLOB GET /orders-scoring": "POST /orders-scoring takes the same ids in a body",
    "CLOB PUT /balance-allowance": "GET /balance-allowance/update refreshes the same cache",
    "GAMMA GET /events/pagination": "/events/keyset pages the same events with stable cursors",
}

# Deliberately not exposed, with the reason.
WAIVED = {
    "CLOB POST /heartbeats": "needs a heartbeat every 5 seconds; one call from a tool only arms "
    "cancel-all for the whole API key",
    "CLOB POST /v1/heartbeats": "needs a heartbeat every 5 seconds; one call from a tool only "
    "arms cancel-all for the whole API key",
    "COMBOS POST /v1/maker/quotes": "market making needs a 400 ms quote loop, not tool calls",
    "COMBOS POST /v1/maker/quotes/cancel": "market making needs a 400 ms quote loop",
    "COMBOS POST /v1/maker/confirmations": "last look must answer within one second",
    "PERPS PATCH /v1/account/builder": "the signed payload of updateBuilder is not documented",
}

# Tracked, and not exposed yet.
PLANNED: dict[str, str] = {}

# Routes this package calls that the published specifications do not list, with where they
# are documented.
EXTRA = {
    "CLOB GET /v1/user/session-signers": "trading/session-keys guide",
    "RELAYER GET /v1/account/transactions/params": "trading/wallets-auth guide",
    "RELAYER GET /v1/account/transactions/{}": "trading/wallets-auth guide",
    "RELAYER POST /v1/session-signers/authorizations": "trading/session-keys guide",
    "RELAYER POST /v1/session-signers/revocations": "trading/session-keys guide",
    "COMBOS_REQUESTER POST /v1/requester/rfq/requests": "trading/combos/requesters guide",
    "COMBOS_REQUESTER GET /v1/requester/rfq/requests/{}": "trading/combos/requesters guide",
    "COMBOS_REQUESTER POST /v1/requester/rfq/requests/{}/accept": "trading/combos/requesters",
    "COLLATERAL_RETURN POST /v1/collateral-return/plan": "trading/combos/collateral-return",
    "COLLATERAL_RETURN POST /v1/collateral-return/submit": "trading/combos/collateral-return",
    "WEB GET /api/geoblock": "api-reference/geoblock",
    "DOCS GET /llms.txt": "the documentation index",
    "DOCS GET /_llms/{}": "the documentation index",
    "DOCS GET /{}": "documentation pages",
}
