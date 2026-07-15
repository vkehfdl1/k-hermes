"""UNSEQUENCED/FINALIZED CAS + restart finalize + fence/get_session."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent.direct_desktop_runner import (
    STATE_FINALIZED,
    STATE_UNSEQUENCED,
    allocate_unsequenced_event,
    ensure_direct_desktop_schema,
    fence_acquire,
    fence_drain,
    fence_release,
    finalize_event_key,
    get_session_snapshot,
    list_unsequenced_events,
    max_final_seq,
    process_max_final_seq_request,
    process_pending_sequence_request,
    restart_finalize_unsequenced,
    DispatchState,
    advance_dispatch_to_running,
    get_dispatch_state,
    set_dispatch_state,
    terminalize_dispatch,
)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_direct_desktop_schema(conn)
    yield conn
    conn.close()


SESSION = "018f0aaa-0000-7000-8000-000000000001"
KEY_A = "018f0aaa-0000-7000-8000-0000000000aa"
KEY_B = "018f0aaa-0000-7000-8000-0000000000bb"
KEY_C = "018f0aaa-0000-7000-8000-0000000000cc"


class TestCasIdempotency:
    def test_first_finalize_allocates_seq_zero(self, db):
        assert max_final_seq(db, SESSION) == -1
        allocate_unsequenced_event(db, SESSION, KEY_A, {"type": "task.created"})
        pending = list_unsequenced_events(db, SESSION)
        assert len(pending) == 1
        assert pending[0]["state"] == STATE_UNSEQUENCED
        assert pending[0]["seq"] is None

        finalized = finalize_event_key(db, SESSION, KEY_A)
        assert finalized["state"] == STATE_FINALIZED
        assert finalized["seq"] == 0
        assert max_final_seq(db, SESSION) == 0

    def test_duplicate_finalize_is_idempotent(self, db):
        allocate_unsequenced_event(db, SESSION, KEY_A)
        first = finalize_event_key(db, SESSION, KEY_A)
        second = finalize_event_key(db, SESSION, KEY_A)
        third = finalize_event_key(db, SESSION, KEY_A)
        assert first["seq"] == second["seq"] == third["seq"] == 0
        assert first["state"] == second["state"] == STATE_FINALIZED
        # Only one FINALIZED row for the session.
        assert max_final_seq(db, SESSION) == 0

    def test_monotonic_sequence_across_keys(self, db):
        for key in (KEY_A, KEY_B, KEY_C):
            allocate_unsequenced_event(db, SESSION, key)
        a = finalize_event_key(db, SESSION, KEY_A)
        b = finalize_event_key(db, SESSION, KEY_B)
        c = finalize_event_key(db, SESSION, KEY_C)
        assert (a["seq"], b["seq"], c["seq"]) == (0, 1, 2)
        assert max_final_seq(db, SESSION) == 2

    def test_pending_sequence_request_control_frame(self, db):
        reply = process_pending_sequence_request(
            db,
            {
                "version": "dolshoi.hermes.runner.v1",
                "type": "pending_sequence_request",
                "eventKey": KEY_A,
                "sessionId": SESSION,
            },
        )
        assert reply["type"] == "pending_sequence_finalized"
        assert reply["eventKey"] == KEY_A
        assert reply["sessionId"] == SESSION
        assert reply["seq"] == 0
        # Idempotent replay of the same control frame.
        reply2 = process_pending_sequence_request(
            db,
            {
                "version": "dolshoi.hermes.runner.v1",
                "type": "pending_sequence_request",
                "eventKey": KEY_A,
                "sessionId": SESSION,
            },
        )
        assert reply2["seq"] == 0
        assert max_final_seq(db, SESSION) == 0

    def test_max_final_seq_control_frame(self, db):
        empty = process_max_final_seq_request(
            db, {"type": "max_final_seq_request", "sessionId": SESSION}
        )
        assert empty["maxFinalSeq"] == -1
        process_pending_sequence_request(
            db,
            {
                "type": "pending_sequence_request",
                "eventKey": KEY_A,
                "sessionId": SESSION,
            },
        )
        filled = process_max_final_seq_request(
            db, {"type": "max_final_seq_request", "sessionId": SESSION}
        )
        assert filled["type"] == "max_final_seq_result"
        assert filled["maxFinalSeq"] == 0

    def test_session_mismatch_raises(self, db):
        allocate_unsequenced_event(db, SESSION, KEY_A)
        finalize_event_key(db, SESSION, KEY_A)
        with pytest.raises(ValueError, match="session mismatch"):
            finalize_event_key(db, "018f0aaa-0000-7000-8000-000000000099", KEY_A)


class TestRestartUnsequencedFinalize:
    def test_restart_finalizes_lingering_unsequenced(self, db):
        # Simulate pre-crash: UNSEQUENCED rows exist, none FINALIZED.
        allocate_unsequenced_event(db, SESSION, KEY_A, {"t": 1})
        allocate_unsequenced_event(db, SESSION, KEY_B, {"t": 2})
        assert max_final_seq(db, SESSION) == -1
        assert len(list_unsequenced_events(db, SESSION)) == 2

        # Restart reconcilation.
        finalized = restart_finalize_unsequenced(db, SESSION)
        assert len(finalized) == 2
        assert [f["seq"] for f in finalized] == [0, 1]
        assert all(f["state"] == STATE_FINALIZED for f in finalized)
        assert max_final_seq(db, SESSION) == 1
        assert list_unsequenced_events(db, SESSION) == []

        # Re-running restart is a no-op for already-finalized keys.
        again = restart_finalize_unsequenced(db, SESSION)
        assert again == []
        assert max_final_seq(db, SESSION) == 1

        # Re-finalize each key still CAS-idempotent.
        a2 = finalize_event_key(db, SESSION, KEY_A)
        b2 = finalize_event_key(db, SESSION, KEY_B)
        assert a2["seq"] == 0
        assert b2["seq"] == 1

    def test_restart_preserves_already_finalized(self, db):
        allocate_unsequenced_event(db, SESSION, KEY_A)
        finalize_event_key(db, SESSION, KEY_A)  # seq 0 live
        allocate_unsequenced_event(db, SESSION, KEY_B)  # crash before finalize
        assert len(list_unsequenced_events(db, SESSION)) == 1

        finalized = restart_finalize_unsequenced(db, SESSION)
        assert len(finalized) == 1
        assert finalized[0]["eventKey"] == KEY_B
        assert finalized[0]["seq"] == 1
        assert max_final_seq(db, SESSION) == 1

    def test_file_db_survives_reopen(self, tmp_path: Path):
        """UNSEQUENCED rows rematerialize after connection reopen (restart)."""
        path = tmp_path / "state.db"
        conn1 = sqlite3.connect(str(path))
        conn1.row_factory = sqlite3.Row
        ensure_direct_desktop_schema(conn1)
        allocate_unsequenced_event(conn1, SESSION, KEY_A)
        allocate_unsequenced_event(conn1, SESSION, KEY_B)
        conn1.close()

        conn2 = sqlite3.connect(str(path))
        conn2.row_factory = sqlite3.Row
        pending = list_unsequenced_events(conn2, SESSION)
        assert len(pending) == 2
        out = restart_finalize_unsequenced(conn2, SESSION)
        assert [o["seq"] for o in out] == [0, 1]
        conn2.close()

        conn3 = sqlite3.connect(str(path))
        conn3.row_factory = sqlite3.Row
        assert max_final_seq(conn3, SESSION) == 1
        assert list_unsequenced_events(conn3, SESSION) == []
        # Idempotent reopen finalize.
        assert restart_finalize_unsequenced(conn3, SESSION) == []
        conn3.close()


class TestFenceAndSnapshot:
    def test_fence_lifecycle_and_replay_watermark(self, db):
        process_pending_sequence_request(
            db,
            {
                "type": "pending_sequence_request",
                "eventKey": KEY_A,
                "sessionId": SESSION,
            },
        )
        process_pending_sequence_request(
            db,
            {
                "type": "pending_sequence_request",
                "eventKey": KEY_B,
                "sessionId": SESSION,
            },
        )
        ack = fence_acquire(db, SESSION)
        assert ack["status"] == "acquired"
        ack = fence_drain(db, SESSION)
        assert ack["status"] == "drained"
        snap = get_session_snapshot(db, SESSION, acquire_fence=False)
        assert snap["replayWatermark"] == 1
        assert snap["sessionId"] == SESSION
        ack = fence_release(db, SESSION)
        assert ack["status"] == "released"

    def test_get_session_auto_fence(self, db):
        allocate_unsequenced_event(db, SESSION, KEY_A)
        finalize_event_key(db, SESSION, KEY_A)
        snap = get_session_snapshot(db, SESSION, acquire_fence=True)
        assert snap["replayWatermark"] == 0


class TestDispatchStateMachine:
    NONCE = "nonce-1"
    TASK = "018f0aaa-0000-7000-8000-0000000000dd"

    def test_committed_to_terminal_path(self, db):
        row = set_dispatch_state(
            db, self.NONCE, SESSION, self.TASK, DispatchState.COMMITTED
        )
        assert row["state"] == "COMMITTED"
        row = advance_dispatch_to_running(db, self.NONCE, SESSION, self.TASK)
        assert row["state"] == "RUNNING"
        row = terminalize_dispatch(
            db, self.NONCE, SESSION, self.TASK, reason="completed"
        )
        assert row["state"] == "TERMINAL"
        assert row["terminalReason"] == "completed"
        # Idempotent re-terminalize
        row2 = terminalize_dispatch(
            db, self.NONCE, SESSION, self.TASK, reason="completed"
        )
        assert row2["state"] == "TERMINAL"

    def test_illegal_transition_rejected(self, db):
        set_dispatch_state(
            db, self.NONCE, SESSION, self.TASK, DispatchState.COMMITTED
        )
        with pytest.raises(ValueError, match="illegal dispatch transition"):
            set_dispatch_state(
                db, self.NONCE, SESSION, self.TASK, DispatchState.RUNNING
            )
        # UNCLAIMED is legal next step
        set_dispatch_state(
            db, self.NONCE, SESSION, self.TASK, DispatchState.UNCLAIMED
        )
        assert get_dispatch_state(db, self.NONCE)["state"] == "UNCLAIMED"
