"""A local record of every write this server sends, readable only by the user.

It is written before a request goes out. When the outcome is unknown (the connection dropped
after the request may have arrived) the exact bytes are kept, so the same request can be sent
again: exchanges reject a duplicate signed request, while a freshly signed one would be a new
order or a second transfer. Bodies are deleted once the outcome is known or they expire.
"""

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

# Signed Perps requests are valid for five minutes; keep bodies a little longer than that.
BODY_TTL_SECONDS = 15 * 60
# A send that has not finished after this long belongs to a process that died mid-request.
STALE_PENDING_SECONDS = 120
# Outcomes that still block an identical write: in flight, unknown, or refused only on resend.
BLOCKING = ("pending", "unknown", "resend_rejected")
# A refused resend says nothing about the first attempt, which may have gone through, so it
# still counts toward the daily limit.
SPENDING = ("pending", "sent", "unknown", "resend_rejected")

WriteState = Literal[
    "pending",
    "sent",
    "rejected",
    "failed",
    "unknown",
    "resend_rejected",
    "cleared",
]

VALID_WRITE_STATES: frozenset[str] = frozenset(
    {"pending", "sent", "rejected", "failed", "unknown", "resend_rejected", "cleared"}
)

WRITE_TRANSITIONS: dict[WriteState | None, frozenset[WriteState]] = {
    None: frozenset({"pending"}),
    "pending": frozenset({"sent", "rejected", "failed", "unknown", "cleared"}),
    "unknown": frozenset({"sent", "resend_rejected", "failed", "cleared"}),
    "sent": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
    "resend_rejected": frozenset(),
    "cleared": frozenset(),
}


def validate_transition(current: str | None, target: str) -> WriteState:
    """Ensure a write journal transition follows WRITE_TRANSITIONS."""
    if current is not None and current not in VALID_WRITE_STATES:
        raise ValueError(f"Unknown current journal state {current!r}.")
    if target not in VALID_WRITE_STATES:
        raise ValueError(f"Unknown target journal state {target!r}.")
    cur = cast("WriteState | None", current)
    tgt = cast("WriteState", target)
    allowed = WRITE_TRANSITIONS[cur]
    if tgt not in allowed:
        raise ValueError(f"Illegal journal state transition: {current!r} -> {target!r}.")
    return tgt


_now = time.time


@dataclass
class Entry:
    id: str
    created: float
    tool: str
    endpoint: str
    summary: str
    usd: str | None
    status: str
    fingerprint: str | None
    body: bytes | None
    response: str | None


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None)
        if os.name == "posix" and path.exists():
            os.chmod(path, 0o600)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS writes (
                id TEXT PRIMARY KEY, created REAL, tool TEXT, endpoint TEXT, summary TEXT,
                usd TEXT, status TEXT, fingerprint TEXT, body BLOB, response TEXT)"""
        )
        # The process that sent these died before learning the outcome: that is unknown.
        self.db.execute(
            "UPDATE writes SET status = 'unknown' WHERE status = 'pending' AND created < ?",
            (_now() - STALE_PENDING_SECONDS,),
        )

    def close(self) -> None:
        self.db.close()

    def record(
        self,
        *,
        tool: str,
        endpoint: str,
        summary: str,
        usd: Decimal | None,
        fingerprint: str | None,
        body: bytes | None,
    ) -> str:
        initial = validate_transition(None, "pending")
        entry_id = uuid.uuid4().hex[:16]
        self.db.execute(
            "INSERT INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (entry_id, _now(), tool, endpoint, summary, _str(usd), initial, fingerprint, body),
        )
        return entry_id

    def finish(self, entry_id: str, status: str, response: Any = None) -> None:
        row = self.db.execute("SELECT status FROM writes WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown journal entry {entry_id!r}.")
        next_state = validate_transition(str(row[0]), status)
        keep_body = next_state == "unknown"
        self.db.execute(
            """
            UPDATE writes
            SET status = ?, response = ?, body = CASE WHEN ? THEN body END
            WHERE id = ?
            """,
            (next_state, _json(response), keep_body, entry_id),
        )

    def clear_unknown(self, entry_id: str, detail: str = "resolved by operator") -> None:
        """Mark an unknown journal entry cleared once checked out-of-band."""
        self.finish(entry_id, "cleared", {"detail": detail})

    def attach_body(self, entry_id: str, body: bytes) -> None:
        self.db.execute("UPDATE writes SET body = ? WHERE id = ?", (body, entry_id))

    def get(self, entry_id: str) -> Entry | None:
        self._expire()
        row = self.db.execute("SELECT * FROM writes WHERE id = ?", (entry_id,)).fetchone()
        return Entry(*row) if row else None

    def unknown(self, limit: int = 20) -> list[Entry]:
        self._expire()
        rows = self.db.execute(
            "SELECT * FROM writes WHERE status = 'unknown' ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Entry(*row) for row in rows]

    def pending_duplicate(self, fingerprint: str) -> Entry | None:
        """An identical write still in flight, or whose outcome became unknown, in the last 15
        minutes.

        That is as long as its body is kept for a resend; after that the block lifts, and the
        daily limit still counts it.
        """
        self._expire()
        row = self.db.execute(
            """
            SELECT * FROM writes
            WHERE fingerprint = ? AND status IN (?, ?, ?) AND created >= ?
            ORDER BY created DESC LIMIT 1
            """,
            (fingerprint, *BLOCKING, _now() - BODY_TTL_SECONDS),
        ).fetchone()
        return Entry(*row) if row else None

    def spent_today(self, kind: str) -> Decimal:
        """USD committed today (UTC) by writes of this kind that went out or may have."""
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.db.execute(
            """
            SELECT usd FROM writes
            WHERE created >= ? AND tool LIKE ? AND usd IS NOT NULL AND status IN (?, ?, ?, ?)
            """,
            (midnight.timestamp(), f"{kind}%", *SPENDING),
        ).fetchall()
        return sum((Decimal(usd) for (usd,) in rows), Decimal(0))

    def _expire(self) -> None:
        self.db.execute(
            "UPDATE writes SET body = NULL WHERE body IS NOT NULL AND created < ?",
            (_now() - BODY_TTL_SECONDS,),
        )


def _str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]
