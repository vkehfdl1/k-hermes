"""Quota, GC, soft-delete DB paths, and open/reveal permission boundaries."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from agent.direct_desktop_media import AttachmentSpec, DirectDesktopMediaService, STATE_COMMITTED
from agent.direct_desktop_store import (
    DirectDesktopMediaStore,
    InMemoryKeyBackend,
    MediaAccessDenied,
    MediaNotFound,
    QuotaExceeded,
)
from hermes_state import SessionDB


def _u(n: int) -> str:
    return f"018f0aaa-0000-7000-8000-{n:012x}"


@pytest.fixture()
def env(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    opened: list = []
    revealed: list = []
    store = DirectDesktopMediaStore(
        tmp_path / "home",
        profile_id="p",
        key_backend=InMemoryKeyBackend(),
        profile_quota=50_000,
        session_quota=20_000,
        open_impl=lambda p: opened.append(p),
        reveal_impl=lambda p: revealed.append(p),
    )
    svc = DirectDesktopMediaService(db, store)
    return svc, db, store, opened, revealed


def _commit_one(svc, *, n, data: bytes = b"payload"):
    session_id, task_id, message_id, attachment_id = _u(n), _u(n + 1), _u(n + 2), _u(n + 3)
    nonce = f"n-{n}"
    svc.prepare_turn(
        acceptance_nonce=nonce,
        client_request_id=str(uuid.uuid4()),
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        prompt="p",
        attachments=[
            AttachmentSpec(
                attachment_id=attachment_id,
                ordinal=0,
                mime_type="text/plain",
                display_name="f.txt",
                content_sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        ],
    )
    svc.finalize_intent(nonce)
    return session_id, attachment_id


def test_open_reveal_authorize_and_no_path(env):
    svc, db, store, opened, revealed = env
    session_id, attachment_id = _commit_one(svc, n=100, data=b"open-me")
    client = str(uuid.uuid4())
    res = svc.open_media(
        client_action_id=client, session_id=session_id, attachment_id=attachment_id
    )
    assert res["version"] == "kmanus.hermes.media-action-result.v1"
    assert res["action"] == "open"
    assert res["attachmentId"] == attachment_id
    assert res["state"] in {"claimed", "completed"}
    assert "path" not in res
    assert opened  # OS open invoked with private path

    res2 = svc.reveal_media(
        client_action_id=str(uuid.uuid4()),
        session_id=session_id,
        attachment_id=attachment_id,
    )
    assert res2["action"] == "reveal"
    assert "path" not in res2
    assert revealed

    # Wrong session denied
    with pytest.raises(MediaAccessDenied):
        svc.open_media(
            client_action_id=str(uuid.uuid4()),
            session_id=_u(999),
            attachment_id=attachment_id,
        )


def test_soft_delete_blocks_open(env):
    svc, db, store, opened, revealed = env
    session_id, attachment_id = _commit_one(svc, n=200, data=b"del")
    svc.soft_delete_attachment(attachment_id)
    with pytest.raises((MediaAccessDenied, MediaNotFound)):
        svc.open_media(
            client_action_id=str(uuid.uuid4()),
            session_id=session_id,
            attachment_id=attachment_id,
        )
    # Snapshots omit soft-deleted
    snap = svc.get_session_snapshot(session_id, fence=False)
    assert snap["messages"][0]["attachments"] == []
    svc.undelete_attachment(attachment_id)
    snap2 = svc.get_session_snapshot(session_id, fence=False)
    assert snap2["messages"][0]["attachments"][0]["attachmentId"] == attachment_id


def test_session_quota_enforced_on_prepare(env):
    svc, db, store, *_ = env
    big = b"Z" * 8_000
    session_id = _u(300)

    def prep(n, data):
        return svc.prepare_turn(
            acceptance_nonce=f"qn-{n}",
            client_request_id=str(uuid.uuid4()),
            session_id=session_id,
            task_id=_u(310 + n),
            message_id=_u(320 + n),
            prompt="q",
            attachments=[
                AttachmentSpec(
                    attachment_id=_u(330 + n),
                    ordinal=0,
                    mime_type="application/octet-stream",
                    display_name=f"b{n}.bin",
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    data=data,
                )
            ],
        )

    # First few ok until session quota (20k ciphertext incl header) trips.
    with pytest.raises(QuotaExceeded):
        for i in range(10):
            prep(i, big)
            # Finalize to account? quota checks temp writes before promote.
            # Even without finalize, prepare reserves via write_encrypted_temp.


def test_reconcile_startup_resumes_intent(env, tmp_path: Path):
    svc, db, store, *_ = env
    session_id, task_id, message_id, attachment_id = _u(400), _u(401), _u(402), _u(403)
    data = b"resume-me"
    svc.prepare_turn(
        acceptance_nonce="re-1",
        client_request_id=str(uuid.uuid4()),
        session_id=session_id,
        task_id=task_id,
        message_id=message_id,
        prompt="r",
        attachments=[
            AttachmentSpec(
                attachment_id=attachment_id,
                ordinal=0,
                mime_type="text/plain",
                display_name="r.txt",
                content_sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        ],
    )
    # Manually mark finalize intent without completing renames/commit via high-level finalize.
    db.mark_desktop_finalize_intent("re-1")
    row = db.get_desktop_turn_acceptance("re-1")
    assert row["finalize_intent"] == 1
    # Simulate crash: temps still present
    assert any(Path(e["tempPath"]).exists() for e in row["promotion_journal"])
    result = svc.reconcile_startup()
    assert result["resumed"] >= 1
    att = db.get_message_attachment(attachment_id)
    assert att["state"] == STATE_COMMITTED
    assert store.read_final(att["blob_key"]) == data


def test_missing_attachment_not_found(env):
    svc, *_ = env
    with pytest.raises(MediaNotFound):
        svc.open_media(
            client_action_id=str(uuid.uuid4()),
            session_id=_u(1),
            attachment_id=_u(2),
        )
