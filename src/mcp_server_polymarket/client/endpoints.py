"""Every Polymarket HTTP route this package calls.

The client only accepts these constants, so the set below is exactly what the tools can reach.
`tests/test_coverage.py` compares it with the published API specifications.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import quote


class Service(StrEnum):
    GAMMA = "gamma_url"
    CLOB = "clob_url"
    DATA = "data_url"
    RELAYER = "relayer_url"
    BRIDGE = "bridge_url"
    COMBOS = "combos_url"
    COMBOS_REQUESTER = "combos_requester_url"
    COLLATERAL_RETURN = "collateral_return_url"
    PERPS = "perps_url"
    WEB = "web_url"
    DOCS = "docs_url"


class Auth(StrEnum):
    NONE = "none"
    CLOB_L1 = "clob_l1"  # wallet signature over ClobAuth typed data
    CLOB_L2 = "clob_l2"  # HMAC with CLOB API credentials
    RELAYER = "relayer"  # relayer API key, or builder headers
    BUILDER = "builder"  # builder HMAC headers on top of CLOB L2
    PERPS = "perps"  # proxy address and secret headers
    PERPS_OPTIONAL = "perps_optional"
    SIGNED = "signed"  # authorised by a signature inside the body


@dataclass(frozen=True)
class Endpoint:
    service: Service
    method: str
    path: str
    auth: Auth = Auth.NONE
    # Whether a retry after an ambiguous failure is harmless. Defaults to true for GET.
    retry_safe: bool | None = None
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        # `{path}` spans several segments (documentation pages); other placeholders span one.
        regex = re.escape(self.path).replace(r"\{path\}", ".+")
        regex = re.sub(r"\\\{[^/]+?\\\}", "[^/]+", regex)
        object.__setattr__(self, "pattern", re.compile(f"^{regex}$"))

    @property
    def safe_to_retry(self) -> bool:
        return self.method == "GET" if self.retry_safe is None else self.retry_safe

    def render(self, path_args: dict[str, str] | None = None) -> str:
        path = self.path
        for key, value in (path_args or {}).items():
            path = path.replace("{" + key + "}", _segment(value, keep_slashes=key == "path"))
        if "{" in path:
            raise ValueError(f"missing path arguments for {self.path}")
        return path

    @property
    def key(self) -> str:
        return f"{self.service.name} {self.method} {self.path}"


def _segment(value: str, *, keep_slashes: bool = False) -> str:
    return quote(str(value), safe="/" if keep_slashes else "")


S, A = Service, Auth


def _e(service: Service, method: str, path: str, auth: Auth = A.NONE, **kw: bool) -> Endpoint:
    return Endpoint(service, method, path, auth, **kw)


# Gamma: discovery and metadata. Public.
GAMMA_STATUS = _e(S.GAMMA, "GET", "/status")
GAMMA_TEAMS = _e(S.GAMMA, "GET", "/teams")
GAMMA_TEAM = _e(S.GAMMA, "GET", "/teams/{id}")
GAMMA_TAGS = _e(S.GAMMA, "GET", "/tags")
GAMMA_TAG = _e(S.GAMMA, "GET", "/tags/{id}")
GAMMA_TAG_BY_SLUG = _e(S.GAMMA, "GET", "/tags/slug/{slug}")
GAMMA_TAG_RELATIONS = _e(S.GAMMA, "GET", "/tags/{id}/related-tags")
GAMMA_TAG_RELATIONS_BY_SLUG = _e(S.GAMMA, "GET", "/tags/slug/{slug}/related-tags")
GAMMA_TAG_RELATED = _e(S.GAMMA, "GET", "/tags/{id}/related-tags/tags")
GAMMA_TAG_RELATED_BY_SLUG = _e(S.GAMMA, "GET", "/tags/slug/{slug}/related-tags/tags")
GAMMA_EVENTS = _e(S.GAMMA, "GET", "/events")
GAMMA_EVENTS_KEYSET = _e(S.GAMMA, "GET", "/events/keyset")
GAMMA_EVENTS_RESULTS = _e(S.GAMMA, "GET", "/events/results")
GAMMA_EVENT = _e(S.GAMMA, "GET", "/events/{id}")
GAMMA_EVENT_TWEETS = _e(S.GAMMA, "GET", "/events/{id}/tweet-count")
GAMMA_EVENT_COMMENT_COUNT = _e(S.GAMMA, "GET", "/events/{id}/comments/count")
GAMMA_EVENT_TAGS = _e(S.GAMMA, "GET", "/events/{id}/tags")
GAMMA_EVENT_BY_SLUG = _e(S.GAMMA, "GET", "/events/slug/{slug}")
GAMMA_EVENT_CREATORS = _e(S.GAMMA, "GET", "/events/creators")
GAMMA_EVENT_CREATOR = _e(S.GAMMA, "GET", "/events/creators/{id}")
GAMMA_MARKETS = _e(S.GAMMA, "GET", "/markets")
GAMMA_MARKETS_KEYSET = _e(S.GAMMA, "GET", "/markets/keyset")
GAMMA_MARKET = _e(S.GAMMA, "GET", "/markets/{id}")
GAMMA_MARKET_DESCRIPTION = _e(S.GAMMA, "GET", "/markets/{id}/description")
GAMMA_MARKET_TAGS = _e(S.GAMMA, "GET", "/markets/{id}/tags")
GAMMA_MARKET_BY_SLUG = _e(S.GAMMA, "GET", "/markets/slug/{slug}")
GAMMA_MARKETS_INFORMATION = _e(S.GAMMA, "POST", "/markets/information", retry_safe=True)
GAMMA_MARKETS_ABRIDGED = _e(S.GAMMA, "POST", "/markets/abridged", retry_safe=True)
GAMMA_SERIES_LIST = _e(S.GAMMA, "GET", "/series")
GAMMA_SERIES = _e(S.GAMMA, "GET", "/series/{id}")
GAMMA_SERIES_COMMENT_COUNT = _e(S.GAMMA, "GET", "/series/{id}/comments/count")
GAMMA_SERIES_SUMMARY = _e(S.GAMMA, "GET", "/series-summary/{id}")
GAMMA_SERIES_SUMMARY_BY_SLUG = _e(S.GAMMA, "GET", "/series-summary/slug/{slug}")
GAMMA_COMMENTS = _e(S.GAMMA, "GET", "/comments")
GAMMA_COMMENT = _e(S.GAMMA, "GET", "/comments/{id}")
GAMMA_COMMENTS_BY_USER = _e(S.GAMMA, "GET", "/comments/user_address/{user_address}")
GAMMA_PUBLIC_PROFILE = _e(S.GAMMA, "GET", "/public-profile")
GAMMA_PROFILE_BY_USER = _e(S.GAMMA, "GET", "/profiles/user_address/{user_address}")
GAMMA_SPORTS = _e(S.GAMMA, "GET", "/sports")
GAMMA_SPORTS_MARKET_TYPES = _e(S.GAMMA, "GET", "/sports/market-types")
GAMMA_SEARCH = _e(S.GAMMA, "GET", "/public-search")

# CLOB: order books, prices and trading.
CLOB_TIME = _e(S.CLOB, "GET", "/time")
CLOB_BOOK = _e(S.CLOB, "GET", "/book")
CLOB_BOOKS = _e(S.CLOB, "POST", "/books", retry_safe=True)
CLOB_SIMPLIFIED_MARKETS = _e(S.CLOB, "GET", "/simplified-markets")
CLOB_SAMPLING_MARKETS = _e(S.CLOB, "GET", "/sampling-markets")
CLOB_SAMPLING_SIMPLIFIED_MARKETS = _e(S.CLOB, "GET", "/sampling-simplified-markets")
CLOB_MARKET_INFO = _e(S.CLOB, "GET", "/clob-markets/{condition_id}")
CLOB_MARKET_BY_TOKEN = _e(S.CLOB, "GET", "/markets-by-token/{token_id}")
CLOB_LIVE_ACTIVITY = _e(S.CLOB, "POST", "/markets/live-activity", retry_safe=True)
CLOB_LIVE_ACTIVITY_ONE = _e(S.CLOB, "GET", "/markets/live-activity/{condition_id}")
CLOB_REWARDS_CURRENT = _e(S.CLOB, "GET", "/rewards/markets/current")
CLOB_REWARDS_MARKET = _e(S.CLOB, "GET", "/rewards/markets/{condition_id}")
CLOB_REWARDS_MULTI = _e(S.CLOB, "GET", "/rewards/markets/multi")
CLOB_REBATES = _e(S.CLOB, "GET", "/rebates/current")
CLOB_BUILDER_TRADES = _e(S.CLOB, "GET", "/builder/trades")
CLOB_CREATE_API_KEY = _e(S.CLOB, "POST", "/auth/api-key", A.CLOB_L1)
CLOB_DERIVE_API_KEY = _e(S.CLOB, "GET", "/auth/derive-api-key", A.CLOB_L1)
CLOB_API_KEYS = _e(S.CLOB, "GET", "/auth/api-keys", A.CLOB_L1)
CLOB_DELETE_API_KEY = _e(S.CLOB, "DELETE", "/auth/api-key", A.CLOB_L2)
CLOB_CLOSED_ONLY = _e(S.CLOB, "GET", "/auth/ban-status/closed-only", A.CLOB_L2)
CLOB_RATE_LIMIT_TIER = _e(S.CLOB, "GET", "/v1/user/rate-limit-tier", A.CLOB_L2)
CLOB_SESSION_SIGNERS = _e(S.CLOB, "GET", "/v1/user/session-signers", A.CLOB_L2)
CLOB_BUILDER_KEYS = _e(S.CLOB, "GET", "/auth/builder-api-key", A.CLOB_L2)
CLOB_CREATE_BUILDER_KEY = _e(S.CLOB, "POST", "/auth/builder-api-key", A.CLOB_L2)
CLOB_REVOKE_BUILDER_KEY = _e(S.CLOB, "DELETE", "/auth/builder-api-key", A.CLOB_L2)
CLOB_BALANCE_ALLOWANCE = _e(S.CLOB, "GET", "/balance-allowance", A.CLOB_L2)
CLOB_BALANCE_ALLOWANCE_UPDATE = _e(
    S.CLOB, "GET", "/balance-allowance/update", A.CLOB_L2, retry_safe=True
)
CLOB_ORDERS = _e(S.CLOB, "GET", "/data/orders", A.CLOB_L2)
CLOB_ORDER = _e(S.CLOB, "GET", "/data/order/{order_id}", A.CLOB_L2)
CLOB_WALLET_OPEN_ORDERS = _e(S.CLOB, "GET", "/data/wallet-open-orders", A.CLOB_L2)
CLOB_TRADES = _e(S.CLOB, "GET", "/data/trades", A.CLOB_L2)
CLOB_NOTIFICATIONS = _e(S.CLOB, "GET", "/notifications", A.CLOB_L2)
CLOB_DROP_NOTIFICATIONS = _e(S.CLOB, "DELETE", "/notifications", A.CLOB_L2, retry_safe=True)
CLOB_USER_EARNINGS = _e(S.CLOB, "GET", "/rewards/user", A.CLOB_L2)
CLOB_USER_EARNINGS_TOTAL = _e(S.CLOB, "GET", "/rewards/user/total", A.CLOB_L2)
CLOB_USER_REWARD_PERCENTAGES = _e(S.CLOB, "GET", "/rewards/user/percentages", A.CLOB_L2)
CLOB_USER_REWARD_MARKETS = _e(S.CLOB, "GET", "/rewards/user/markets", A.CLOB_L2)
CLOB_ORDER_SCORING = _e(S.CLOB, "GET", "/order-scoring", A.CLOB_L2)
CLOB_ORDERS_SCORING = _e(S.CLOB, "POST", "/orders-scoring", A.CLOB_L2, retry_safe=True)
CLOB_POST_ORDER = _e(S.CLOB, "POST", "/order", A.CLOB_L2)
CLOB_POST_ORDERS = _e(S.CLOB, "POST", "/orders", A.CLOB_L2)
# Cancels are idempotent: cancelling an order twice cancels it once.
CLOB_CANCEL_ORDER = _e(S.CLOB, "DELETE", "/order", A.CLOB_L2, retry_safe=True)
CLOB_CANCEL_ORDERS = _e(S.CLOB, "DELETE", "/orders", A.CLOB_L2, retry_safe=True)
CLOB_CANCEL_ALL = _e(S.CLOB, "DELETE", "/cancel-all", A.CLOB_L2, retry_safe=True)
CLOB_CANCEL_MARKET = _e(S.CLOB, "DELETE", "/cancel-market-orders", A.CLOB_L2, retry_safe=True)
CLOB_CANCEL_WALLET = _e(S.CLOB, "DELETE", "/cancel-wallet-orders", A.CLOB_L2, retry_safe=True)

# Data API v2: positions, feeds and boards. Public.
DATA_POSITIONS = _e(S.DATA, "GET", "/v2/positions")
DATA_COMBO_POSITIONS = _e(S.DATA, "GET", "/v2/positions/combos")
DATA_VALUE = _e(S.DATA, "GET", "/v2/value")
DATA_APPROVALS = _e(S.DATA, "GET", "/v2/approvals")
DATA_USER_PNL = _e(S.DATA, "GET", "/v2/user-pnl")
DATA_USER_STATS = _e(S.DATA, "GET", "/v2/user-stats")
DATA_USER_VOLUME = _e(S.DATA, "GET", "/v2/user-volume")
DATA_TRADES = _e(S.DATA, "GET", "/v2/trades")
DATA_ACTIVITY = _e(S.DATA, "GET", "/v2/activity")
DATA_COMBO_ACTIVITY = _e(S.DATA, "GET", "/v2/activity/combos")
DATA_HOLDERS = _e(S.DATA, "GET", "/v2/holders")
DATA_OPEN_INTEREST = _e(S.DATA, "GET", "/v2/oi")
DATA_LIVE_VOLUME = _e(S.DATA, "GET", "/v2/live-volume")
DATA_PRICES_HISTORY = _e(S.DATA, "GET", "/v2/prices-history")
DATA_RESOLUTIONS = _e(S.DATA, "GET", "/v2/resolutions")
DATA_TOKENS = _e(S.DATA, "GET", "/v2/tokens")
DATA_LEADERBOARD = _e(S.DATA, "GET", "/v2/leaderboard")
DATA_BIGGEST_WINNERS = _e(S.DATA, "GET", "/v2/biggest-winners")
DATA_BUILDER_LEADERBOARD = _e(S.DATA, "GET", "/v2/builders/leaderboard")
DATA_BUILDER_VOLUME = _e(S.DATA, "GET", "/v2/builders/volume")
DATA_STATUS = _e(S.DATA, "GET", "/v2/status")

# Relayer: gasless wallet transactions.
RELAYER_SUBMIT = _e(S.RELAYER, "POST", "/submit", A.RELAYER)
RELAYER_TRANSACTION = _e(S.RELAYER, "GET", "/transaction")
RELAYER_TRANSACTIONS = _e(S.RELAYER, "GET", "/transactions", A.RELAYER)
RELAYER_NONCE = _e(S.RELAYER, "GET", "/nonce")
RELAYER_RELAY_PAYLOAD = _e(S.RELAYER, "GET", "/relay-payload")
RELAYER_DEPLOYED = _e(S.RELAYER, "GET", "/deployed")
RELAYER_API_KEYS = _e(S.RELAYER, "GET", "/relayer/api/keys", A.RELAYER)
RELAYER_TX_PARAMS = _e(S.RELAYER, "GET", "/v1/account/transactions/params", A.RELAYER)
RELAYER_TX = _e(S.RELAYER, "GET", "/v1/account/transactions/{id}", A.RELAYER)
# Session keys are authorised with an Idempotency-Key, so a retry cannot apply twice.
RELAYER_SESSION_AUTHORIZE = _e(
    S.RELAYER, "POST", "/v1/session-signers/authorizations", A.RELAYER, retry_safe=True
)
RELAYER_SESSION_REVOKE = _e(
    S.RELAYER, "POST", "/v1/session-signers/revocations", A.RELAYER, retry_safe=True
)

# Bridge: deposits and withdrawals from other chains. Public.
BRIDGE_ASSETS = _e(S.BRIDGE, "GET", "/supported-assets")
BRIDGE_QUOTE = _e(S.BRIDGE, "POST", "/quote", retry_safe=True)
BRIDGE_DEPOSIT = _e(S.BRIDGE, "POST", "/deposit", retry_safe=True)
BRIDGE_WITHDRAW = _e(S.BRIDGE, "POST", "/withdraw", retry_safe=True)
BRIDGE_STATUS = _e(S.BRIDGE, "GET", "/status/{address}")

# Combos.
COMBO_MARKETS = _e(S.COMBOS, "GET", "/v1/rfq/combo-markets")
COMBO_REQUEST_CREATE = _e(S.COMBOS_REQUESTER, "POST", "/v1/requester/rfq/requests", A.CLOB_L2)
COMBO_REQUEST = _e(S.COMBOS_REQUESTER, "GET", "/v1/requester/rfq/requests/{id}", A.CLOB_L2)
COMBO_REQUEST_ACCEPT = _e(
    S.COMBOS_REQUESTER, "POST", "/v1/requester/rfq/requests/{id}/accept", A.CLOB_L2
)
COLLATERAL_RETURN_PLAN = _e(
    S.COLLATERAL_RETURN, "POST", "/v1/collateral-return/plan", retry_safe=True
)
COLLATERAL_RETURN_SUBMIT = _e(
    S.COLLATERAL_RETURN, "POST", "/v1/collateral-return/submit", A.RELAYER
)

# Website.
WEB_GEOBLOCK = _e(S.WEB, "GET", "/api/geoblock")

# Documentation, for the docs tools.
DOCS_INDEX = _e(S.DOCS, "GET", "/llms.txt")
DOCS_SUBINDEX = _e(S.DOCS, "GET", "/_llms/{path}")
DOCS_PAGE = _e(S.DOCS, "GET", "/{path}")

# Perps.
P = S.PERPS
PERPS_PING = _e(P, "GET", "/v1/info/ping")
PERPS_TIME = _e(P, "GET", "/v1/info/time")
PERPS_EXCHANGE = _e(P, "GET", "/v1/info/exchange")
PERPS_ASSETS = _e(P, "GET", "/v1/info/assets")
PERPS_INSTRUMENTS = _e(P, "GET", "/v1/info/instruments")
PERPS_TICKERS = _e(P, "GET", "/v1/info/tickers")
PERPS_STATISTICS = _e(P, "GET", "/v1/info/statistics")
PERPS_EXCHANGE_STATS = _e(P, "GET", "/v1/info/exchange-stats")
PERPS_KLINES = _e(P, "GET", "/v1/info/klines")
PERPS_MARK_HISTORY = _e(P, "GET", "/v1/info/mark-history")
PERPS_BBO = _e(P, "GET", "/v1/info/bbo")
PERPS_BOOK = _e(P, "GET", "/v1/info/book")
PERPS_INDEX = _e(P, "GET", "/v1/info/index")
PERPS_TRADES = _e(P, "GET", "/v1/info/trades")
PERPS_PUBLIC_PORTFOLIO = _e(P, "GET", "/v1/info/portfolio")
PERPS_PUBLIC_FILLS = _e(P, "GET", "/v1/info/fills")
PERPS_POSITION_FILLS = _e(P, "GET", "/v1/info/position-fills")
PERPS_FUNDING_HISTORY = _e(P, "GET", "/v1/info/funding")
PERPS_FEES = _e(P, "GET", "/v1/info/fees")
PERPS_LIMIT_TIERS = _e(P, "GET", "/v1/info/limit-tiers")
PERPS_INVITE_CHECK = _e(P, "GET", "/v1/info/invite")
PERPS_LEADERBOARD = _e(P, "GET", "/v1/info/leaderboard")
PERPS_BUILDER_INFO = _e(P, "GET", "/v1/info/builder")
PERPS_REGISTERED = _e(P, "GET", "/v1/info/registered")
PERPS_COMPETITIONS = _e(P, "GET", "/v1/info/competitions")
PERPS_COMPETITION = _e(P, "GET", "/v1/info/competition")
PERPS_COMPETITION_LEADERBOARD = _e(P, "GET", "/v1/info/competition/leaderboard")
PERPS_COMPETITION_ACCOUNT = _e(P, "GET", "/v1/info/competition/account")
PERPS_COMPETITION_WIDGET = _e(P, "GET", "/v1/info/competition/widget")
PERPS_POSITION_SNAPSHOTS = _e(
    P, "POST", "/v1/info/position-snapshots", A.PERPS_OPTIONAL, retry_safe=True
)
PERPS_CREATE_ORDERS = _e(P, "POST", "/v1/trade/orders", A.SIGNED)
PERPS_CANCEL_ORDERS = _e(P, "DELETE", "/v1/trade/orders", A.SIGNED)
PERPS_MODIFY_ORDERS = _e(P, "PATCH", "/v1/trade/orders", A.SIGNED)
PERPS_CANCEL_ALL = _e(P, "DELETE", "/v1/trade/orders/all", A.SIGNED)
PERPS_CANCEL_ORDERS_COID = _e(P, "DELETE", "/v1/trade/orders-coid", A.SIGNED)
PERPS_MODIFY_ORDERS_COID = _e(P, "PATCH", "/v1/trade/orders-coid", A.SIGNED)
PERPS_AUTO_CANCEL = _e(P, "PATCH", "/v1/trade/auto-cancel", A.SIGNED)
PERPS_LEVERAGE = _e(P, "PATCH", "/v1/trade/leverage", A.SIGNED)
PERPS_LEVERAGE_BATCH = _e(P, "PATCH", "/v1/trade/leverage/batch", A.SIGNED)
PERPS_MARGIN = _e(P, "PATCH", "/v1/trade/margin", A.SIGNED)
PERPS_CREATE_PROXY = _e(P, "POST", "/v1/account/proxy", A.SIGNED)
PERPS_DELETE_PROXY = _e(P, "DELETE", "/v1/account/proxy", A.SIGNED)
PERPS_CREDENTIALS = _e(P, "GET", "/v1/account/credentials", A.PERPS)
PERPS_ORDERS = _e(P, "GET", "/v1/account/orders", A.PERPS)
PERPS_OPEN_ORDERS = _e(P, "GET", "/v1/account/open-orders", A.PERPS)
PERPS_BALANCES = _e(P, "GET", "/v1/account/balances", A.PERPS)
PERPS_PORTFOLIO = _e(P, "GET", "/v1/account/portfolio", A.PERPS)
PERPS_FILLS = _e(P, "GET", "/v1/account/fills", A.PERPS)
PERPS_EQUITY = _e(P, "GET", "/v1/account/equity", A.PERPS)
PERPS_PNL = _e(P, "GET", "/v1/account/pnl", A.PERPS)
PERPS_BACKSTOPS = _e(P, "GET", "/v1/account/backstops", A.PERPS)
PERPS_FUNDING = _e(P, "GET", "/v1/account/funding", A.PERPS)
PERPS_WITHDRAW = _e(P, "POST", "/v1/account/withdraw", A.SIGNED)
PERPS_INTERNAL_TRANSFER = _e(P, "POST", "/v1/account/internal-transfer", A.SIGNED)
PERPS_DEPOSITS = _e(P, "GET", "/v1/account/deposits", A.PERPS)
PERPS_INTERNAL_TRANSFERS = _e(P, "GET", "/v1/account/internal-transfers", A.PERPS)
PERPS_WITHDRAWALS = _e(P, "GET", "/v1/account/withdrawals", A.PERPS)
PERPS_ACCOUNT_STATS = _e(P, "GET", "/v1/account/stats", A.PERPS)
PERPS_ACCOUNT_LIMITS = _e(P, "GET", "/v1/account/limits", A.PERPS)
PERPS_ACCOUNT_REWARDS = _e(P, "GET", "/v1/account/rewards", A.PERPS)
PERPS_ACCOUNT_CONFIG = _e(P, "GET", "/v1/account/config", A.PERPS)
PERPS_AUTO_CANCEL_STATUS = _e(P, "GET", "/v1/account/auto-cancel", A.PERPS)
PERPS_CREATE_INVITE = _e(P, "POST", "/v1/account/invite", A.PERPS, retry_safe=True)
PERPS_REFERRAL = _e(P, "GET", "/v1/account/referral", A.PERPS)
PERPS_APPLY_REFERRAL = _e(P, "POST", "/v1/account/referral", A.PERPS)
PERPS_NOTIFICATIONS = _e(P, "GET", "/v1/account/notifications", A.PERPS)
PERPS_NOTIFICATIONS_READ = _e(P, "POST", "/v1/account/notifications/read", A.PERPS, retry_safe=True)
PERPS_BLP_ENROLLMENT = _e(P, "GET", "/v1/blp/enrollment", A.PERPS)
PERPS_BLP_ENROLL = _e(P, "POST", "/v1/blp/enroll", A.PERPS, retry_safe=True)
PERPS_BLP_LIQUIDATIONS = _e(P, "GET", "/v1/blp/liquidations", A.PERPS)
PERPS_BUILDER_EARNINGS = _e(P, "GET", "/v1/account/builder-earnings", A.PERPS)
PERPS_BUILDER_EARNINGS_SUMMARY = _e(P, "GET", "/v1/account/builder-earnings-summary", A.PERPS)
PERPS_BUILDER_APPROVALS = _e(P, "GET", "/v1/account/builder-approvals", A.PERPS)
PERPS_APPROVE_BUILDER = _e(P, "POST", "/v1/account/builder-approvals", A.SIGNED)

ALL: tuple[Endpoint, ...] = tuple(
    value for value in list(globals().values()) if isinstance(value, Endpoint)
)


def find(service: Service, method: str, path: str) -> Endpoint | None:
    """The endpoint a concrete request belongs to, preferring literal routes over templates."""
    candidates = [
        e for e in ALL if e.service == service and e.method == method and e.pattern.match(path)
    ]
    candidates.sort(key=lambda e: e.path.count("{"))
    return candidates[0] if candidates else None
