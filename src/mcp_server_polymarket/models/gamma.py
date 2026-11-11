"""Gamma objects, trimmed to what an agent needs. Upstream sends ~90 keys per market."""

from typing import Any

from mcp_server_polymarket.models.base import (
    CompactModel,
    as_dict,
    as_float,
    as_int,
    as_list,
    iso_time,
    json_list,
)
from mcp_server_polymarket.redaction import clean_text

DESCRIPTION_LIMIT = 300
SITE = "https://polymarket.com"


class Outcome(CompactModel):
    name: str
    price: float | None = None
    token_id: str | None = None


class TagRef(CompactModel):
    id: str | None = None
    label: str | None = None
    slug: str | None = None


def _tags(raw: Any) -> list[TagRef] | None:
    if not isinstance(raw, list) or not raw:
        return None
    return [
        TagRef(
            id=str(tag.get("id")) if tag.get("id") is not None else None,
            label=tag.get("label"),
            slug=tag.get("slug"),
        )
        for tag in raw
        if isinstance(tag, dict)
    ]


def outcomes_of(market: dict[str, Any]) -> list[Outcome]:
    names = json_list(market.get("outcomes"))
    prices = json_list(market.get("outcomePrices"))
    # Polymarket Protocol V2 markets trade their position ids; older markets their CLOB tokens.
    tokens = json_list(market.get("positionIds")) if market.get("version") == "v2" else []
    tokens = tokens or json_list(market.get("clobTokenIds"))
    result = []
    for index, name in enumerate(names):
        result.append(
            Outcome(
                name=str(name),
                price=as_float(prices[index]) if index < len(prices) else None,
                token_id=str(tokens[index]) if index < len(tokens) else None,
            )
        )
    return result


def _status(raw: dict[str, Any]) -> str | None:
    if raw.get("archived"):
        return "archived"
    if raw.get("closed"):
        return "closed"
    if raw.get("active"):
        return "active"
    return None


class Market(CompactModel):
    """A binary market. `outcomes` pairs each outcome with its price (probability) and token."""

    id: str
    slug: str | None = None
    question: str | None = None
    group_item_title: str | None = None
    condition_id: str | None = None
    question_id: str | None = None
    outcomes: list[Outcome] = []
    status: str | None = None
    accepting_orders: bool | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    last_trade_price: float | None = None
    one_day_price_change: float | None = None
    volume: float | None = None
    volume_24h: float | None = None
    liquidity: float | None = None
    start_date: str | None = None
    end_date: str | None = None
    closed_time: str | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    rfq_enabled: bool | None = None
    fees_enabled: bool | None = None
    fee_schedule: dict[str, Any] | None = None
    rewards_min_size: float | None = None
    rewards_max_spread: float | None = None
    protocol: str | None = None
    event_id: str | None = None
    event_slug: str | None = None
    url: str | None = None
    resolution_source: str | None = None
    uma_resolution_status: str | None = None
    tags: list[TagRef] | None = None
    description: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, full: bool = False) -> "Market":
        events = as_list(raw.get("events"))
        event = events[0] if events and isinstance(events[0], dict) else {}
        event_slug = event.get("slug")
        url = (
            f"{SITE}/event/{event_slug}/{raw.get('slug')}"
            if event_slug and raw.get("slug")
            else None
        )
        statuses = json_list(raw.get("umaResolutionStatuses"))
        return cls(
            id=str(raw.get("id")),
            slug=raw.get("slug"),
            question=clean_text(raw.get("question")),
            group_item_title=clean_text(raw.get("groupItemTitle")) or None,
            condition_id=raw.get("conditionId") or None,
            question_id=raw.get("questionID") or None,
            outcomes=outcomes_of(raw),
            status=_status(raw),
            accepting_orders=raw.get("acceptingOrders"),
            best_bid=as_float(raw.get("bestBid")),
            best_ask=as_float(raw.get("bestAsk")),
            spread=as_float(raw.get("spread")),
            last_trade_price=as_float(raw.get("lastTradePrice")),
            one_day_price_change=as_float(raw.get("oneDayPriceChange")),
            volume=as_float(raw.get("volumeNum") or raw.get("volume")),
            volume_24h=as_float(raw.get("volume24hr")),
            liquidity=as_float(raw.get("liquidityNum") or raw.get("liquidity")),
            start_date=iso_time(raw.get("startDate")),
            end_date=iso_time(raw.get("endDate")),
            closed_time=iso_time(raw.get("closedTime")),
            tick_size=as_float(raw.get("orderPriceMinTickSize")),
            min_order_size=as_float(raw.get("orderMinSize")),
            neg_risk=raw.get("negRisk"),
            rfq_enabled=raw.get("rfqEnabled") or None,
            fees_enabled=raw.get("feesEnabled"),
            fee_schedule=raw.get("feeSchedule") if full else None,
            rewards_min_size=as_float(raw.get("rewardsMinSize")) or None,
            rewards_max_spread=as_float(raw.get("rewardsMaxSpread")) or None,
            protocol=raw.get("version"),
            event_id=str(event["id"]) if event.get("id") is not None else None,
            event_slug=event_slug,
            url=url,
            resolution_source=(raw.get("resolutionSource") or None) if full else None,
            uma_resolution_status=str(statuses[-1]) if statuses else None,
            tags=_tags(raw.get("tags")) if full else None,
            description=clean_text(raw.get("description"), None if full else DESCRIPTION_LIMIT)
            or None,
        )


class MarketLine(CompactModel):
    """A market inside an event: just enough to pick one and look it up."""

    id: str
    slug: str | None = None
    title: str | None = None
    condition_id: str | None = None
    outcomes: list[Outcome] = []
    status: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    volume: float | None = None
    volume_24h: float | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "MarketLine":
        return cls(
            id=str(raw.get("id")),
            slug=raw.get("slug"),
            title=clean_text(raw.get("groupItemTitle") or raw.get("question")),
            condition_id=raw.get("conditionId") or None,
            outcomes=outcomes_of(raw),
            status=_status(raw),
            best_bid=as_float(raw.get("bestBid")),
            best_ask=as_float(raw.get("bestAsk")),
            volume=as_float(raw.get("volumeNum") or raw.get("volume")),
            volume_24h=as_float(raw.get("volume24hr")),
        )


class Event(CompactModel):
    """An event groups related markets, e.g. one market per candidate."""

    id: str
    slug: str | None = None
    title: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    volume: float | None = None
    volume_24h: float | None = None
    liquidity: float | None = None
    open_interest: float | None = None
    neg_risk: bool | None = None
    comment_count: int | None = None
    series_slug: str | None = None
    url: str | None = None
    tags: list[TagRef] | None = None
    markets_total: int | None = None
    markets: list[MarketLine] = []
    description: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, full: bool = False, max_markets: int = 10) -> "Event":
        markets = [m for m in raw.get("markets") or [] if isinstance(m, dict)]
        # Most traded first, so a capped list keeps the markets that matter.
        markets.sort(
            key=lambda m: as_float(m.get("volumeNum") or m.get("volume")) or 0, reverse=True
        )
        series = as_list(raw.get("series"))
        slug = raw.get("slug")
        return cls(
            id=str(raw.get("id")),
            slug=slug,
            title=clean_text(raw.get("title")),
            status=_status(raw),
            start_date=iso_time(raw.get("startDate")),
            end_date=iso_time(raw.get("endDate")),
            volume=as_float(raw.get("volume")),
            volume_24h=as_float(raw.get("volume24hr")),
            liquidity=as_float(raw.get("liquidity")),
            open_interest=as_float(raw.get("openInterest")),
            neg_risk=raw.get("negRisk"),
            comment_count=as_int(raw.get("commentCount")),
            series_slug=series[0].get("slug") if series and isinstance(series[0], dict) else None,
            url=f"{SITE}/event/{slug}" if slug else None,
            tags=_tags(raw.get("tags")),
            markets_total=len(markets),
            markets=[MarketLine.from_api(m) for m in markets[:max_markets]],
            description=clean_text(raw.get("description"), None if full else DESCRIPTION_LIMIT)
            or None,
        )


class Tag(CompactModel):
    id: str
    label: str | None = None
    slug: str | None = None
    is_carousel: bool | None = None
    force_show: bool | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Tag":
        return cls(
            id=str(raw.get("id")),
            label=raw.get("label"),
            slug=raw.get("slug"),
            is_carousel=raw.get("isCarousel") or None,
            force_show=raw.get("forceShow") or None,
        )


class TagRelation(CompactModel):
    tag_id: str | None = None
    related_tag_id: str | None = None
    rank: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "TagRelation":
        return cls(
            tag_id=str(raw.get("tagID")) if raw.get("tagID") is not None else None,
            related_tag_id=str(raw.get("relatedTagID"))
            if raw.get("relatedTagID") is not None
            else None,
            rank=as_int(raw.get("rank")),
        )


class Series(CompactModel):
    id: str
    slug: str | None = None
    title: str | None = None
    series_type: str | None = None
    recurrence: str | None = None
    status: str | None = None
    volume: float | None = None
    volume_24h: float | None = None
    liquidity: float | None = None
    comment_count: int | None = None
    event_dates: list[str] | None = None
    events_total: int | None = None
    events: list[Event] | None = None
    description: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, max_events: int = 0) -> "Series":
        events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
        # Upcoming and live first: the newest events by end date.
        events.sort(key=lambda e: str(e.get("endDate") or ""), reverse=True)
        dates = raw.get("eventDates")
        return cls(
            id=str(raw.get("id")),
            slug=raw.get("slug"),
            title=clean_text(raw.get("title")),
            series_type=raw.get("seriesType"),
            recurrence=raw.get("recurrence"),
            status=_status(raw),
            volume=as_float(raw.get("volume")),
            volume_24h=as_float(raw.get("volume24hr")),
            liquidity=as_float(raw.get("liquidity")),
            comment_count=as_int(raw.get("commentCount")),
            event_dates=dates[-30:] if isinstance(dates, list) and dates else None,
            events_total=len(events) if events else None,
            events=[Event.from_api(e, max_markets=3) for e in events[:max_events]]
            if max_events and events
            else None,
            description=clean_text(raw.get("description"), DESCRIPTION_LIMIT) or None,
        )


class Comment(CompactModel):
    """A user comment. Its text is untrusted user content."""

    id: str
    body: str | None = None
    author: str | None = None
    user_address: str | None = None
    parent_entity: str | None = None
    reply_to: str | None = None
    created_at: str | None = None
    reactions: int | None = None
    positions: list[dict[str, Any]] | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Comment":
        profile = as_dict(raw.get("profile"))
        positions = as_list(profile.get("positions")) or None
        entity = None
        if raw.get("parentEntityType"):
            entity = f"{raw.get('parentEntityType')}:{raw.get('parentEntityID')}"
        return cls(
            id=str(raw.get("id")),
            body=clean_text(raw.get("body"), 1000),
            author=profile.get("name") or profile.get("pseudonym"),
            user_address=raw.get("userAddress"),
            parent_entity=entity,
            reply_to=str(raw["parentCommentID"]) if raw.get("parentCommentID") else None,
            created_at=iso_time(raw.get("createdAt")),
            reactions=as_int(raw.get("reactionCount")) or None,
            positions=positions or None,
        )


class Profile(CompactModel):
    wallet: str | None = None
    name: str | None = None
    pseudonym: str | None = None
    bio: str | None = None
    created_at: str | None = None
    verified: bool | None = None
    taker_tier: str | None = None
    weighted_volume: float | None = None
    x_username: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Profile":
        return cls(
            wallet=raw.get("proxyWallet"),
            name=clean_text(raw.get("name")),
            pseudonym=raw.get("pseudonym"),
            bio=clean_text(raw.get("bio"), 300) or None,
            created_at=iso_time(raw.get("createdAt")),
            verified=raw.get("verifiedBadge"),
            taker_tier=raw.get("takerTierName"),
            weighted_volume=as_float(raw.get("weightedVolume")),
            x_username=raw.get("xUsername") or None,
        )


class EventCreator(CompactModel):
    id: str
    name: str | None = None
    handle: str | None = None
    url: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "EventCreator":
        return cls(
            id=str(raw.get("id")),
            name=raw.get("creatorName"),
            handle=raw.get("creatorHandle"),
            url=raw.get("creatorUrl"),
        )
