"""Session restore / snapshot / 2PC prepare-finalize / watermark tests."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from agent.direct_desktop_media import (
    AttachmentSpec,
    DirectDesktopMediaService,
    STATE_COMMITTED,
    STATE_PREPARED,
    replay_watermark,
)
from agent.direct_desktop_store import DirectDesktopMediaStore, InMemoryKeyBackend
from hermes_state import SessionDB


def _uuid7(n: int = 1) -> str:
    # Not a real UUIDv7, but matches the protocol lowercase hex pattern used in practice.
    return f"018f0aaa-0000-7000-8000-{n:012x}"


@pytest.fixture()
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


@pytest.fixture()
def service(tmp_path: Path, db: SessionDB) -> DirectDesktopMediaService:
    store = DirectDesktopMediaStore(
        tmp_path / "home",
        profile_id="prof",
        key_backend=InMemoryKeyBackend(),
        open_impl=lambda p: None,
        reveal_impl=lambda p: None,
    )
    return DirectDesktopMediaService(db, store)


def test_prepare_finalize_ordering_and_snapshot(service: DirectDesktopMediaService, db: SessionDB):
    session_id = _uuid7(1)
    task_id = _uuid7(2)
    message_id = _uuid7(3)
    attachment_id = _uuid7(4)
    nonce = "nonce-1"
    client = str(uuid.uuid4())
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    prepared = service.prepare_turn(
        acceptance_nonce=nonce,
        client_request_id=client,
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        prompt="describe",
        attachments=[
            AttachmentSpec(
                attachment_id=attachment_id,
                ordinal=0,
                mime_type="image/png",
                display_name="scan.png",
                content_sha256=__import__("hashlib").sha256(png).hexdigest(),
                data=png,
                direction="input",
            )
        ],
    )
    assert prepared["type"] == "turn.prepared"
    # PREPARED attachments not visible in committed snapshot yet.
    snap0 = service.get_session_snapshot(session_id, fence=False)
    assert snap0["version"] == "dolshoi.hermes.session.v1"
    assert snap0["sessionId"] == session_id
    assert "replayWatermark" in snap0
    # Message exists in task tables but attachments empty until COMMITTED.
    assert snap0["messages"]
    assert snap0["messages"][0]["attachments"] == []

    # Pre-intent status
    st = service.status_turn(nonce)
    assert st["state"] == STATE_PREPARED

    # finalize intent + renames + COMMITTED
    accepted = service.finalize_intent(nonce)
    assert accepted["type"] == "turn.accepted"
    assert accepted["attachments"][0]["attachmentId"] == attachment_id
    assert accepted["attachments"][0]["displayName"] == "scan.png"

    # Blob exists under media/blobs and decrypts.
    blob_key = db.get_message_attachment(attachment_id)["blob_key"]
    assert (service.store.blobs_dir / blob_key).is_file()
    assert service.store.read_final(blob_key) == png

    snap = service.get_session_snapshot(session_id, fence=True)
    assert snap["messages"][0]["messageId"] == message_id
    assert snap["messages"][0]["taskId"] == task_id
    att = snap["messages"][0]["attachments"][0]
    assert att["attachmentId"] == attachment_id
    assert att["direction"] == "input"
    assert att["previewAvailable"] is True
    assert snap["tasks"][0]["taskId"] == task_id
    assert message_id in snap["tasks"][0]["messageIds"]
    assert isinstance(snap["replayWatermark"], int)


def test_finalize_idempotent_and_abort_after_intent(service: DirectDesktopMediaService):
    session_id = _uuid7(11)
    task_id = _uuid7(12)
    message_id = _uuid7(13)
    attachment_id = _uuid7(14)
    nonce = "nonce-2"
    data = b"hello-world"
    service.prepare_turn(
        acceptance_nonce=nonce,
        client_request_id=str(uuid.uuid4()),
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        prompt="hi",
        attachments=[
            AttachmentSpec(
                attachment_id=attachment_id,
                ordinal=0,
                mime_type="text/plain",
                display_name="note.txt",
                content_sha256=__import__("hashlib").sha256(data).hexdigest(),
                data=data,
            )
        ],
    )
    a1 = service.finalize_intent(nonce)
    a2 = service.finalize_intent(nonce)
    assert a1["type"] == a2["type"] == "turn.accepted"

    aborted = service.abort_turn(nonce)
    assert aborted.get("abortResult") == "finalize_won" or aborted.get("type") == "turn.accepted"


def test_abort_before_intent_deletes_temps(service: DirectDesktopMediaService, tmp_path: Path):
    session_id = _uuid7(21)
    task_id = _uuid7(22)
    message_id = _uuid7(23)
    attachment_id = _uuid7(24)
    nonce = "nonce-3"
    data = b"temp-only"
    service.prepare_turn(
        acceptance_nonce=nonce,
        client_request_id=str(uuid.uuid4()),
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        prompt="x",
        attachments=[
            AttachmentSpec(
                attachment_id=attachment_id,
                ordinal=0,
                mime_type="text/plain",
                display_name="t.txt",
                content_sha256=__import__("hashlib").sha256(data).hexdigest(),
                data=data,
            )
        ],
    )
    row = service.db.get_desktop_turn_acceptance(nonce)
    temps = [Path(e["tempPath"]) for e in row["promotion_journal"]]
    assert all(t.exists() for t in temps)
    out = service.abort_turn(nonce)
    assert out["state"] == "ABORTED"
    assert all(not t.exists() for t in temps)


def test_replay_watermark_from_cas(db: SessionDB, service: DirectDesktopMediaService):
    session_id = _uuid7(31)
    # finalize a couple of events
    r1 = db.finalize_event_key(session_id, "ek-1")
    r2 = db.finalize_event_key(session_id, "ek-2")
    assert r1["seq"] == 0
    assert r2["seq"] == 1
    # idempotent
    r1b = db.finalize_event_key(session_id, "ek-1")
    assert r1b["seq"] == 0
    assert db.max_final_seq(session_id) == 1
    assert replay_watermark(db, session_id) == 1
    snap = service.get_session_snapshot(session_id, fence=True)
    assert snap["replayWatermark"] == 1


def test_fence_hooks(db: SessionDB):
    sid = _uuid7(41)
    assert db.fence_session(sid, "acquire")["status"] == "acquired"
    assert db.drain_session_fence(sid)["status"] == "drained"
    assert db.fence_session(sid, "release")["status"] == "free"
