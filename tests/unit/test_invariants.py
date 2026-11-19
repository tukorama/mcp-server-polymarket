"""Property-based and stateful invariant tests for write journal, rounding, and text hygiene."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from mcp_server_polymarket.journal import (
    BLOCKING,
    SPENDING,
    VALID_WRITE_STATES,
    WRITE_TRANSITIONS,
    Journal,
    WriteState,
    validate_transition,
)
from mcp_server_polymarket.models.base import (
    as_decimal_str,
    as_dict,
    as_list,
    dict_items,
    require_dict,
)
from mcp_server_polymarket.redaction import clean_text, register_secret, scrub

_TERMINAL_OUTCOMES: tuple[WriteState, ...] = ("sent", "rejected", "failed", "cleared")
_RESEND_OUTCOMES: tuple[WriteState, ...] = ("sent", "resend_rejected", "failed", "cleared")


class WriteJournalStateMachine(RuleBasedStateMachine):
    """Verify every write-journal state transition, spend accounting, and file permission."""

    def __init__(self) -> None:
        super().__init__()
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self.db_path: Path | None = None
        self.journal: Journal | None = None
        self.states: dict[str, WriteState] = {}
        self.fingerprints: dict[str, str] = {}
        self.amounts: dict[str, Decimal] = {}
        self.seq = 0

    @initialize()
    def init_journal(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "journal.sqlite3"
        self.journal = Journal(self.db_path)
        self.states.clear()
        self.fingerprints.clear()
        self.amounts.clear()
        self.seq = 0
        if os.name != "nt":
            mode = self.db_path.stat().st_mode & 0o777
            assert mode == 0o600

    @rule(
        usd_cents=st.integers(min_value=1, max_value=50_000),
        fp_tag=st.integers(min_value=1, max_value=6),
    )
    def record_write(self, usd_cents: int, fp_tag: int) -> None:
        assert self.journal is not None
        self.seq += 1
        usd = Decimal(usd_cents) / Decimal(100)
        fp = f"fp-{fp_tag}"
        entry_id = self.journal.record(
            tool="trading:place_limit_order",
            endpoint="CLOB POST /order",
            summary=f"write-{self.seq}",
            usd=usd,
            fingerprint=fp,
            body=b'{"order":1}',
        )
        assert entry_id not in self.states
        self.states[entry_id] = "pending"
        self.fingerprints[entry_id] = fp
        self.amounts[entry_id] = usd

    @precondition(lambda self: any(s == "pending" for s in self.states.values()))
    @rule(
        choice=st.sampled_from([*list(_TERMINAL_OUTCOMES), "unknown"]),
        pick=st.integers(min_value=0, max_value=100),
    )
    def finish_pending(self, choice: WriteState, pick: int) -> None:
        assert self.journal is not None
        pending_ids = sorted(k for k, v in self.states.items() if v == "pending")
        entry_id = pending_ids[pick % len(pending_ids)]
        self.journal.finish(entry_id, choice, response={"status": choice})
        self.states[entry_id] = choice

    @precondition(lambda self: any(s == "unknown" for s in self.states.values()))
    @rule(
        choice=st.sampled_from(_RESEND_OUTCOMES),
        pick=st.integers(min_value=0, max_value=100),
    )
    def finish_unknown(self, choice: WriteState, pick: int) -> None:
        assert self.journal is not None
        unknown_ids = sorted(k for k, v in self.states.items() if v == "unknown")
        entry_id = unknown_ids[pick % len(unknown_ids)]
        self.journal.finish(entry_id, choice, response={"resolved": choice})
        self.states[entry_id] = choice

    @precondition(lambda self: any(s == "unknown" for s in self.states.values()))
    @rule(pick=st.integers(min_value=0, max_value=100))
    def clear_unknown_entry(self, pick: int) -> None:
        assert self.journal is not None
        unknown_ids = sorted(k for k, v in self.states.items() if v == "unknown")
        entry_id = unknown_ids[pick % len(unknown_ids)]
        self.journal.clear_unknown(entry_id, detail="verified by user")
        entry = self.journal.get(entry_id)
        assert entry is not None
        assert entry.status == "cleared"
        assert entry.body is None
        self.states[entry_id] = "cleared"

    @precondition(
        lambda self: any(
            s in {"sent", "rejected", "failed", "resend_rejected", "cleared"}
            for s in self.states.values()
        )
    )
    @rule(
        next_state=st.sampled_from(sorted(VALID_WRITE_STATES)),
        pick=st.integers(min_value=0, max_value=100),
    )
    def reject_terminal_transition(self, next_state: str, pick: int) -> None:
        assert self.journal is not None
        terminal_ids = sorted(k for k, v in self.states.items() if not WRITE_TRANSITIONS[v])
        entry_id = terminal_ids[pick % len(terminal_ids)]
        with pytest.raises(ValueError, match="Illegal journal state transition"):
            self.journal.finish(entry_id, next_state)

    @invariant()
    def verify_state(self) -> None:
        if self.journal is None:
            return
        expected_unknown = {k for k, v in self.states.items() if v == "unknown"}
        actual_unknown = {e.id for e in self.journal.unknown()}
        assert actual_unknown == expected_unknown

        expected_spent = sum(
            (self.amounts[k] for k, v in self.states.items() if v in SPENDING),
            Decimal(0),
        )
        assert self.journal.spent_today("trading:") == expected_spent

        for fp in set(self.fingerprints.values()):
            matching_blocking = [
                k for k, v in self.states.items() if self.fingerprints[k] == fp and v in BLOCKING
            ]
            dup = self.journal.pending_duplicate(fp)
            if matching_blocking:
                assert dup is not None
                assert dup.id in matching_blocking
            else:
                assert dup is None

    def teardown(self) -> None:
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None


TestWriteJournalStateMachine = WriteJournalStateMachine.TestCase
TestWriteJournalStateMachine.settings = settings(max_examples=35, stateful_step_count=18)


@given(current=st.one_of(st.none(), st.text(max_size=20)), target=st.text(max_size=20))
def test_validate_transition_matches_fsm_table(current: str | None, target: str) -> None:
    if (
        (current is None or current in VALID_WRITE_STATES)
        and target in VALID_WRITE_STATES
        and target in WRITE_TRANSITIONS.get(cast("WriteState | None", current), frozenset())
    ):
        assert validate_transition(current, target) == target
    else:
        with pytest.raises(ValueError, match="journal state"):
            validate_transition(current, target)


@given(value=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("100000"), places=4))
def test_as_decimal_str_round_trips_without_scientific_notation(value: Decimal) -> None:
    rendered = as_decimal_str(str(value))
    assert rendered is not None
    assert "e" not in rendered.lower()
    assert Decimal(rendered) == value


@given(payload=st.one_of(st.none(), st.integers(), st.text(), st.lists(st.integers())))
def test_model_converters_fail_safe_on_arbitrary_payloads(payload: Any) -> None:
    assert as_dict(payload) == {}
    if isinstance(payload, list):
        assert as_list(payload) == payload
        assert dict_items(payload) == []
    else:
        assert as_list(payload) == []
        assert dict_items(payload) == []
    if payload is None:
        assert require_dict(payload) == {}
    else:
        with pytest.raises(TypeError, match="Expected JSON object"):
            require_dict(payload)


@given(
    secret=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=12, max_size=32
    ),
    untrusted=st.text(max_size=120),
)
def test_scrub_and_clean_text_invariants(secret: str, untrusted: str) -> None:
    register_secret(secret)
    scrubbed = scrub(f"header secret={secret} trailer")
    assert secret not in scrubbed
    cleaned = clean_text(untrusted, 40)
    assert cleaned is not None
    assert len(cleaned) <= 40
    assert "\x00" not in cleaned
    assert "\u202e" not in cleaned
