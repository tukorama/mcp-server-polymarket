"""Data API v2 rows. The API already answers in compact snake_case, so rows pass through, minus
images and empty display fields."""

from typing import Any

from pydantic import ConfigDict

from mcp_server_polymarket.models.base import CompactModel, iso_time
from mcp_server_polymarket.redaction import clean_text

_DROP = {
    "icon",
    "image",
    "profile_image",
    "profile_image_optimized",
    "bio",
    "display_username_public",
}
_TEXT = {"title", "name", "pseudonym", "question"}
_TIMES = {
    "timestamp",
    "last_event_at",
    "first_entry_at",
    "join_date",
    "resolved_at",
    "created_at",
    "updated_at",
    "last_update_timestamp",
    "computed_at",
}


def slim(row: Any) -> Any:
    """Drop images and empty values, mark unlabelled outcomes, turn epoch times into ISO."""
    if isinstance(row, list):
        return [slim(item) for item in row]
    if not isinstance(row, dict):
        return row
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key in _DROP or value is None or value == "":
            continue
        if key == "outcome_index" and value == 999:
            continue
        if key in _TEXT and isinstance(value, str):
            value = clean_text(value, 200)
        elif key in _TIMES and isinstance(value, int | float | str):
            value = iso_time(value) or value
        elif isinstance(value, dict | list):
            value = slim(value)
        result[key] = value
    return result


class DataRow(CompactModel):
    """A Data API row. Amounts: bare sizes are shares, `_usdc` fields are USD."""

    model_config = ConfigDict(extra="allow")


class Position(DataRow):
    proxy_wallet: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    title: str | None = None
    outcome: str | None = None
    status: str | None = None
    current_size: float | None = None
    avg_price: float | None = None
    current_price: float | None = None
    current_value: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    redeemable: bool | None = None
    mergeable: bool | None = None


class Trade(DataRow):
    proxy_wallet: str | None = None
    side: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    outcome: str | None = None
    size: float | None = None
    price: float | None = None
    timestamp: str | None = None
    title: str | None = None
    transaction_hash: str | None = None


class Activity(DataRow):
    type: str | None = None
    timestamp: str | None = None
    condition_id: str | None = None
    size: float | None = None
    usdc_size: float | None = None
    title: str | None = None


def rows(model: type[DataRow], data: Any) -> list[Any]:
    if not isinstance(data, list):
        return []
    return [model.model_validate(slim(item)) for item in data if isinstance(item, dict)]
