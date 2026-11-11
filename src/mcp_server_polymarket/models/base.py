from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

T = TypeVar("T")


class CompactModel(BaseModel):
    """Leaves nulls out of tool output, since every key costs agent context."""

    model_config = ConfigDict(extra="ignore")

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if value is not None}


class Raw(CompactModel):
    """An upstream object passed through as-is, for responses that are already compact."""

    model_config = ConfigDict(extra="allow")


class CursorPage(CompactModel, Generic[T]):
    """One page. Pass `next_cursor` back as `cursor` to continue; absent means the end."""

    items: list[T] = []
    next_cursor: str | None = None
    has_more: bool | None = None
    count: int | None = None
    note: str | None = None


class Section(CompactModel):
    """Part of a combined answer that failed, so the rest can still be used."""

    section: str
    error: str


class WriteResult(CompactModel):
    """What happened to a write. Deliberately loose: a successful send never becomes an error."""

    model_config = ConfigDict(extra="allow")

    status: str
    message: str | None = None
    request_id: str | None = None
    next_step: str | None = None
    dry_run: bool | None = None
    raw: Any = None


# Converters shared by the models.


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_decimal_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except ArithmeticError:
        return str(value)


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def iso_time(value: Any) -> str | None:
    """ISO 8601 UTC from an ISO string or epoch seconds or milliseconds."""
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.lstrip("-").isdigit():
            return stripped
        value = int(stripped)
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def json_list(value: Any) -> list[Any]:
    """Gamma sends some arrays as JSON-encoded strings."""
    import json

    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []
