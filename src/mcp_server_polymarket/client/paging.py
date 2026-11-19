"""One cursor vocabulary for APIs that page by keyset cursor, offset or time window."""

from datetime import UTC, datetime

from mcp_server_polymarket.errors import InvalidRequest

_OFFSET = "o:"


def offset_cursor(offset: int) -> str:
    return f"{_OFFSET}{offset}"


def read_offset(cursor: str | None) -> int:
    """The offset a synthesised cursor stands for; no cursor means the first page."""
    if not cursor:
        return 0
    if not cursor.startswith(_OFFSET) or not cursor[len(_OFFSET) :].isdigit():
        raise InvalidRequest(
            "This cursor does not belong to this tool.",
            hint="Pass next_cursor exactly as the previous page returned it, or leave it empty.",
        )
    return int(cursor[len(_OFFSET) :])


def next_offset(offset: int, received: int, limit: int) -> str | None:
    """A full page may have more after it; a short page is the last."""
    return offset_cursor(offset + received) if received >= limit and received > 0 else None


def epoch_seconds(value: str | int | float | None) -> int | None:
    """Epoch seconds from ISO 8601 (date or date-time) or a number of seconds or milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return epoch_seconds(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRequest(
            f"{value!r} is not a time.", hint="Use ISO 8601, e.g. 2026-11-01T12:00:00Z."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def epoch_ms(value: str | int | float | None) -> int | None:
    seconds = epoch_seconds(value)
    if seconds is None:
        return None
    if isinstance(value, int | float) and value > 10_000_000_000:
        return int(value)
    return seconds * 1000


def iso_param(value: str | None) -> str | None:
    """Gamma takes ISO 8601 dates; accept epoch input too."""
    seconds = epoch_seconds(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
