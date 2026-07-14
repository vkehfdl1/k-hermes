"""Direct-desktop media plane high-level APIs (2PC prepare/finalize, open/reveal).

Bridges runner ingest, SessionDB attachment tables, and the encrypted store:

* PREPARED: encrypt temp + DB state (hidden)
* finalize_intent: durable winner point (journal) before renames
* renames: promote temps to content-addressed finals
* COMMITTED: durable visible message/attachment rows

Open/reveal APIs never return filesystem paths to callers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent.direct_desktop_store import (
    DirectDesktopMediaStore,
    InMemoryKeyBackend,
    MediaAccessDenied,
    MediaNotFound,
    MediaOpenFailed,
    MediaStoreError,
    PreparedObject,
    QuotaExceeded,
    content_address,
)

logger = logging.getLogger(__name__)

STATE_PREPARED = "PREPARED"
STATE_FINALIZE_INTENT = "FINALIZE_INTENT"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"

PREPARED_TTL_SECONDS = 15 * 60


class TurnAcceptanceError(Exception):
    code = "media_persist_failed"

    def __init__(self, message: str = "", *, code: Optional[str] = None):
        super().__init__(message or self.code)
        if code:
            self.code = code


@dataclass
class AttachmentSpec:
    attachment_id: str
    ordinal: int
    mime_type: str
    display_name: str
    content_sha256: str
    data: bytes
    direction: str = "input"  # input | output


@dataclass
class PreparedTurn:
    acceptance_nonce: str
    client_request_id: str
    session_id: str
    task_id: str
    message_id: str
    prompt: str
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    prepared_objects: List[PreparedObject] = field(default_factory=list)
    prepared_previews: List[PreparedObject] = field(default_factory=list)
    fingerprint: str = ""
    created_at: float = 0.0


class DirectDesktopMediaService:
    """Profile-scoped media service owned by k-hermes."""

    def __init__(
        self,
        session_db: Any,
        store: DirectDesktopMediaStore,
        *,
        open_impl: Optional[Any] = None,
        reveal_impl: Optional[Any] = None,
    ) -> None:
        self.db = session_db
        self.store = store
        if open_impl is not None:
            self.store._open_impl = open_impl
        if reveal_impl is not None:
            self.store._reveal_impl = reveal_impl

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def open_default(
        cls,
        session_db: Any,
        hermes_home: Optional[Path] = None,
        *,
        profile_id: str = "default",
        key_backend: Optional[Any] = None,
    ) -> "DirectDesktopMediaService":
        store = DirectDesktopMediaStore.open_default(
            hermes_home,
            profile_id=profile_id,
            key_backend=key_backend or InMemoryKeyBackend(),
        )
        return cls(session_db, store)

    # ── 2PC prepare / finalize / abort ───────────────────────────────────────

    def prepare_turn(
        self,
        *,
        acceptance_nonce: str,
        client_request_id: str,
        session_id: str,
        task_id: str,
        message_id: str,
        prompt: str,
        attachments: Sequence[AttachmentSpec | Mapping[str, Any]],
        source: str = "desktop",
    ) -> Dict[str, Any]:
        """Encrypt attachment temps + insert PREPARED rows (not yet visible).

        Ordering inside this call (plan stage-05)::
          1. fsync encrypted temps
          2. DB transaction inserting PREPARED turn/message/attachments
          3. return turn.prepared metadata (finalize still pending)
        """
        specs = [_coerce_spec(a) for a in attachments]
        fingerprint = _request_fingerprint(
            client_request_id=client_request_id,
            session_id=session_id,
            task_id=task_id,
            message_id=message_id,
            prompt=prompt,
            attachments=specs,
        )

        # Idempotency: identical nonce already prepared/committed.
        existing = self.db.get_desktop_turn_acceptance(acceptance_nonce)
        if existing is not None:
            if existing.get("request_fingerprint") not in (None, fingerprint) and existing.get(
                "request_fingerprint"
            ) != fingerprint:
                raise TurnAcceptanceError(
                    "acceptance nonce fingerprint conflict",
                    code="media_request_conflict",
                )
            return self._prepared_frame_from_row(existing)

        prepared_objects: List[PreparedObject] = []
        prepared_previews: List[PreparedObject] = []
        attachment_records: List[Dict[str, Any]] = []

        try:
            for spec in specs:
                actual = content_address(spec.data)
                if actual != spec.content_sha256.lower():
                    raise TurnAcceptanceError(
                        "contentSha256 mismatch", code="media_persist_failed"
                    )
                # Reserve + write temp with fsync.
                prepared = self.store.write_encrypted_temp(
                    spec.data,
                    mime_type=spec.mime_type,
                    kind="blob",
                    blob_key=actual,
                    session_id=session_id,
                    reserve_quota=True,
                )
                prepared_objects.append(prepared)
                preview_available = False
                preview_key = None
                if spec.mime_type.startswith("image/") and len(spec.data) <= 5_242_880:
                    try:
                        prev = self.store.write_encrypted_temp(
                            spec.data,
                            mime_type=spec.mime_type,
                            kind="preview",
                            blob_key=actual,
                            session_id=session_id,
                            reserve_quota=True,
                        )
                        prepared_previews.append(prev)
                        preview_available = True
                        preview_key = prev.blob_key
                    except QuotaExceeded:
                        pass

                attachment_records.append(
                    {
                        "attachment_id": spec.attachment_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "task_id": task_id,
                        "ordinal": spec.ordinal,
                        "mime_type": spec.mime_type,
                        "display_name": spec.display_name,
                        "content_sha256": actual,
                        "byte_size": len(spec.data),
                        "ciphertext_size": prepared.ciphertext_size,
                        "blob_key": prepared.blob_key,
                        "preview_key": preview_key,
                        "preview_available": 1 if preview_available else 0,
                        "direction": spec.direction,
                        "state": STATE_PREPARED,
                        "key_id": prepared.key_id,
                        "key_version": prepared.key_version,
                        "temp_path": str(prepared.temp_path),
                        "preview_temp_path": (
                            str(prepared_previews[-1].temp_path)
                            if preview_key and prepared_previews
                            else None
                        ),
                    }
                )

            objects_json = [
                {
                    "blobKey": o.blob_key,
                    "kind": o.kind,
                    "tempPath": str(o.temp_path),
                    "ciphertextSize": o.ciphertext_size,
                    "keyId": o.key_id,
                    "keyVersion": o.key_version,
                    "mimeType": o.mime_type,
                }
                for o in [*prepared_objects, *prepared_previews]
            ]

            self.db.insert_prepared_desktop_turn(
                acceptance_nonce=acceptance_nonce,
                client_request_id=client_request_id,
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
                prompt=prompt,
                request_fingerprint=fingerprint,
                source=source,
                attachments=attachment_records,
                promotion_journal=objects_json,
            )
        except Exception:
            for o in prepared_objects + prepared_previews:
                self.store.abort_temp(o)
            raise

        return {
            "version": "kmanus.hermes.runner.v1",
            "type": "turn.prepared",
            "acceptanceNonce": acceptance_nonce,
            "clientRequestId": client_request_id,
            "sessionId": session_id,
            "taskId": task_id,
            "messageId": message_id,
            "attachments": [
                {
                    "attachmentId": r["attachment_id"],
                    "ordinal": r["ordinal"],
                    "mimeType": r["mime_type"],
                    "displayName": r["display_name"],
                    "previewAvailable": bool(r["preview_available"]),
                }
                for r in attachment_records
            ],
        }

    def finalize_intent(self, acceptance_nonce: str) -> Dict[str, Any]:
        """Mark finalize_intent=1 — the durable winner point (plan stage-05).

        After this returns successfully, abort cannot delete data and must
        report COMMITTED/finalize_won once renames complete.
        """
        row = self.db.get_desktop_turn_acceptance(acceptance_nonce)
        if row is None:
            raise TurnAcceptanceError("unknown acceptance nonce", code="media_not_found")
        state = row.get("state")
        if state == STATE_COMMITTED:
            return self._accepted_frame_from_row(row)
        if state == STATE_ABORTED:
            raise TurnAcceptanceError("turn already aborted", code="media_cancelled_before_acceptance")
        if row.get("finalize_intent"):
            # Resume promotions + commit if needed.
            return self._complete_promotion(acceptance_nonce, row)

        self.db.mark_desktop_finalize_intent(acceptance_nonce)
        row = self.db.get_desktop_turn_acceptance(acceptance_nonce)
        assert row is not None
        return self._complete_promotion(acceptance_nonce, row)

    def _complete_promotion(self, acceptance_nonce: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        journal = row.get("promotion_journal") or []
        if isinstance(journal, str):
            try:
                journal = json.loads(journal)
            except Exception:
                journal = []

        # Promote objects; mark each journal entry promoted.
        for entry in journal:
            if entry.get("promoted"):
                continue
            prepared = PreparedObject(
                blob_key=entry["blobKey"],
                temp_path=Path(entry["tempPath"]),
                ciphertext_size=int(entry.get("ciphertextSize") or 0),
                key_id=str(entry.get("keyId") or self.store.key_id),
                key_version=int(entry.get("keyVersion") or self.store.key_version),
                mime_type=str(entry.get("mimeType") or "application/octet-stream"),
                plaintext_size=0,
                content_sha256=entry["blobKey"],
                kind=str(entry.get("kind") or "blob"),
            )
            self.store.promote_temp(prepared)
            entry["promoted"] = True
            self.db.update_desktop_promotion_journal(acceptance_nonce, journal)

        # Visibility transaction: PREPARED → COMMITTED.
        committed = self.db.commit_desktop_turn(acceptance_nonce)
        # Account session bytes.
        session_id = committed["session_id"]
        for entry in journal:
            if entry.get("kind") != "preview":
                self.store.account_session_bytes(
                    session_id, int(entry.get("ciphertextSize") or 0)
                )
        return self._accepted_frame_from_row(committed)

    def abort_turn(self, acceptance_nonce: str) -> Dict[str, Any]:
        row = self.db.get_desktop_turn_acceptance(acceptance_nonce)
        if row is None:
            return {"state": "UNKNOWN", "acceptanceNonce": acceptance_nonce}
        if row.get("state") == STATE_COMMITTED or row.get("finalize_intent"):
            # finalize_won
            if row.get("state") != STATE_COMMITTED:
                # Resume commit path.
                try:
                    return {
                        **self._complete_promotion(acceptance_nonce, row),
                        "abortResult": "finalize_won",
                    }
                except Exception:
                    return {
                        "state": STATE_COMMITTED,
                        "acceptanceNonce": acceptance_nonce,
                        "abortResult": "finalize_won",
                    }
            return {
                **self._accepted_frame_from_row(row),
                "abortResult": "finalize_won",
            }

        journal = row.get("promotion_journal") or []
        if isinstance(journal, str):
            try:
                journal = json.loads(journal)
            except Exception:
                journal = []
        for entry in journal:
            try:
                p = Path(entry.get("tempPath") or "")
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        self.db.abort_desktop_turn(acceptance_nonce)
        return {
            "version": "kmanus.hermes.runner.v1",
            "op": "abort",
            "acceptanceNonce": acceptance_nonce,
            "clientRequestId": row.get("client_request_id"),
            "sessionId": row.get("session_id"),
            "state": STATE_ABORTED,
            "abortResult": "aborted",
        }

    def status_turn(self, acceptance_nonce: str) -> Dict[str, Any]:
        row = self.db.get_desktop_turn_acceptance(acceptance_nonce)
        if row is None:
            return {
                "version": "kmanus.hermes.runner.v1",
                "op": "status",
                "acceptanceNonce": acceptance_nonce,
                "state": "UNKNOWN",
            }
        state = row.get("state") or "UNKNOWN"
        # Resume incomplete finalize on status.
        if row.get("finalize_intent") and state != STATE_COMMITTED:
            try:
                frame = self._complete_promotion(acceptance_nonce, row)
                state = STATE_COMMITTED
                row = self.db.get_desktop_turn_acceptance(acceptance_nonce) or row
                return {
                    "version": "kmanus.hermes.runner.v1",
                    "op": "status",
                    "acceptanceNonce": acceptance_nonce,
                    "clientRequestId": row.get("client_request_id"),
                    "sessionId": row.get("session_id"),
                    "state": state,
                    "taskId": row.get("task_id"),
                    "messageId": row.get("message_id"),
                    "attachments": frame.get("attachments") or [],
                }
            except Exception as exc:
                logger.warning("status resume failed: %s", exc)
        return {
            "version": "kmanus.hermes.runner.v1",
            "op": "status",
            "acceptanceNonce": acceptance_nonce,
            "clientRequestId": row.get("client_request_id"),
            "sessionId": row.get("session_id"),
            "state": state,
            "taskId": row.get("task_id"),
            "messageId": row.get("message_id"),
            "attachments": self.db.list_message_attachments(
                row.get("message_id") or "", include_prepared=True
            ),
        }

    def reconcile_startup(self) -> Dict[str, int]:
        """Resume finalize_intent promotions; abort expired PREPARED turns."""
        resumed = 0
        aborted = 0
        now = time.time()
        for row in self.db.list_desktop_turns_for_reconcile():
            nonce = row["acceptance_nonce"]
            if row.get("finalize_intent") and row.get("state") != STATE_COMMITTED:
                try:
                    self._complete_promotion(nonce, row)
                    resumed += 1
                except Exception as exc:
                    logger.warning("reconcile promote failed for %s: %s", nonce, exc)
                continue
            if (
                row.get("state") == STATE_PREPARED
                and not row.get("finalize_intent")
                and now - float(row.get("created_at") or now) > PREPARED_TTL_SECONDS
            ):
                self.abort_turn(nonce)
                aborted += 1
        # Cleanup exports + trash.
        expired_exports = self.store.cleanup_expired_exports(now=now)
        purged_trash = self.store.gc_trash(now=now)
        return {
            "resumed": resumed,
            "aborted": aborted,
            "expiredExports": expired_exports,
            "purgedTrash": purged_trash,
        }

    # ── open / reveal ────────────────────────────────────────────────────────

    def open_media(
        self,
        *,
        client_action_id: str,
        session_id: str,
        attachment_id: str,
    ) -> Dict[str, Any]:
        """Authorize + decrypt export lease + native open. Never returns paths."""
        att = self._authorize_attachment(session_id, attachment_id)
        lease = self.store.create_export_lease(
            att["blob_key"],
            display_name=att["display_name"],
            mime_type=att["mime_type"],
            kind="blob",
        )
        try:
            self.store.native_open(lease["leaseId"])
            state = "completed"
        except MediaOpenFailed:
            # Claimed even if OS open fails after lease creation.
            state = "claimed"
            raise
        finally:
            # Keep lease for TTL regardless; GC handles expiry.
            pass
        return {
            "version": "kmanus.hermes.media-action-result.v1",
            "clientActionId": client_action_id,
            "action": "open",
            "attachmentId": attachment_id,
            "state": state if state == "completed" else "claimed",
        }

    def reveal_media(
        self,
        *,
        client_action_id: str,
        session_id: str,
        attachment_id: str,
    ) -> Dict[str, Any]:
        att = self._authorize_attachment(session_id, attachment_id)
        lease = self.store.create_export_lease(
            att["blob_key"],
            display_name=att["display_name"],
            mime_type=att["mime_type"],
            kind="blob",
        )
        try:
            self.store.native_reveal(lease["leaseId"])
            state = "completed"
        except MediaOpenFailed:
            state = "claimed"
            raise
        return {
            "version": "kmanus.hermes.media-action-result.v1",
            "clientActionId": client_action_id,
            "action": "reveal",
            "attachmentId": attachment_id,
            "state": state if state == "completed" else "claimed",
        }

    def _authorize_attachment(self, session_id: str, attachment_id: str) -> Dict[str, Any]:
        att = self.db.get_message_attachment(attachment_id)
        if att is None:
            raise MediaNotFound(f"attachment not found: {attachment_id}")
        if att.get("session_id") != session_id:
            raise MediaAccessDenied("attachment session mismatch")
        if att.get("state") != STATE_COMMITTED or att.get("soft_deleted"):
            raise MediaAccessDenied("attachment not available")
        if not att.get("blob_key"):
            raise MediaNotFound("attachment has no blob")
        return att

    # ── soft delete / undelete / preview ─────────────────────────────────────

    def soft_delete_attachment(self, attachment_id: str) -> None:
        att = self.db.get_message_attachment(attachment_id)
        if att is None:
            raise MediaNotFound(attachment_id)
        if att.get("blob_key"):
            try:
                self.store.soft_delete(att["blob_key"], kind="blob")
            except MediaNotFound:
                pass
        if att.get("preview_key"):
            try:
                self.store.soft_delete(att["preview_key"], kind="preview")
            except MediaNotFound:
                pass
        self.db.soft_delete_message_attachment(attachment_id)

    def undelete_attachment(self, attachment_id: str) -> None:
        att = self.db.get_message_attachment(attachment_id, include_deleted=True)
        if att is None:
            raise MediaNotFound(attachment_id)
        if att.get("blob_key"):
            self.store.undelete(att["blob_key"], kind="blob")
        if att.get("preview_key"):
            try:
                self.store.undelete(att["preview_key"], kind="preview")
            except MediaNotFound:
                pass
        self.db.undelete_message_attachment(attachment_id)

    def ingest_output_artifacts(
        self,
        *,
        session_id: str,
        task_id: str,
        message_id: str,
        artifacts: Sequence[Mapping[str, Any]],
        text: str = "",
    ) -> List[Dict[str, Any]]:
        """Persist agent-produced local files as COMMITTED output attachments.

        Outbound artifacts skip the inbound 2PC: the files already exist on
        local disk under agent control, so each blob (plus an image preview
        when small enough) is encrypted, promoted, and made visible in one
        step, linked to an assistant desktop message. Unreadable, empty, or
        >25MiB files are skipped. Returns ``media.output.created``-shaped
        briefs for the runner to emit.
        """
        import mimetypes

        briefs: List[Dict[str, Any]] = []
        rows: List[Dict[str, Any]] = []
        ordinal = 0
        for art in artifacts:
            path = Path(str(art.get("path") or "")).expanduser()
            try:
                data = path.read_bytes()
            except OSError:
                logger.info("output artifact unreadable, skipping: %s", path)
                continue
            if not data or len(data) > 26_214_400:
                logger.info(
                    "output artifact empty/oversized, skipping: %s (%d bytes)",
                    path,
                    len(data),
                )
                continue
            mime = str(art.get("mime_type") or "").strip() or (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            display = str(art.get("display_name") or path.name)
            sha = content_address(data)
            try:
                prepared = self.store.write_encrypted_temp(
                    data,
                    mime_type=mime,
                    kind="blob",
                    blob_key=sha,
                    session_id=session_id,
                    reserve_quota=True,
                )
                self.store.promote_temp(prepared)
            except (QuotaExceeded, MediaStoreError) as exc:
                logger.warning("output artifact persist failed (%s): %s", path, exc)
                continue
            preview_key = None
            preview_available = False
            if mime.startswith("image/") and len(data) <= 5_242_880:
                try:
                    prev = self.store.write_encrypted_temp(
                        data,
                        mime_type=mime,
                        kind="preview",
                        blob_key=sha,
                        session_id=session_id,
                        reserve_quota=True,
                    )
                    self.store.promote_temp(prev)
                    preview_key = prev.blob_key
                    preview_available = True
                except (QuotaExceeded, MediaStoreError):
                    pass
            self.store.account_session_bytes(session_id, prepared.ciphertext_size)
            attachment_id = str(uuid.uuid4())
            rows.append(
                {
                    "attachment_id": attachment_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "task_id": task_id,
                    "ordinal": ordinal,
                    "mime_type": mime,
                    "display_name": display,
                    "content_sha256": sha,
                    "byte_size": len(data),
                    "ciphertext_size": prepared.ciphertext_size,
                    "blob_key": prepared.blob_key,
                    "preview_key": preview_key,
                    "preview_available": preview_available,
                    "key_id": prepared.key_id,
                    "key_version": prepared.key_version,
                }
            )
            briefs.append(
                {
                    "attachmentId": attachment_id,
                    "ordinal": ordinal,
                    "mimeType": mime,
                    "displayName": display,
                    "previewAvailable": preview_available,
                    "sessionId": session_id,
                    "taskId": task_id,
                    "messageId": message_id,
                }
            )
            ordinal += 1
        if rows:
            self.db.insert_committed_desktop_output(
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
                text=text,
                attachments=rows,
            )
        return briefs

    def get_preview(self, session_id: str, attachment_id: str) -> Dict[str, Any]:
        att = self._authorize_attachment(session_id, attachment_id)
        key = att.get("preview_key") or att.get("blob_key")
        data = self.store.read_final(key, kind="preview" if att.get("preview_key") else "blob")
        if len(data) > 5_242_880:
            raise MediaAccessDenied("preview too large")
        import base64

        encoded = base64.b64encode(data).decode("ascii")
        return {
            "version": "kmanus.hermes.preview.v1",
            "mimeType": att["mime_type"],
            "byteSize": len(data),
            "encodedBase64": encoded,
        }

    # ── session snapshot (protocol kmanus.hermes.session.v1) ─────────────────

    def get_session_snapshot(self, session_id: str, *, fence: bool = True) -> Dict[str, Any]:
        """Durable snapshot after optional fence drain with replayWatermark."""
        if fence:
            fence_acquire = getattr(self.db, "fence_session", None)
            if callable(fence_acquire):
                try:
                    fence_acquire(session_id, "acquire")
                    fence_acquire(session_id, "drain")
                except Exception as exc:
                    logger.debug("fence hook failed: %s", exc)
            else:
                # Prefer runner helpers when SessionDB methods absent.
                try:
                    from agent.direct_desktop_runner import (
                        fence_acquire as _fa,
                        fence_drain as _fd,
                    )

                    _fa(self.db, session_id)
                    _fd(self.db, session_id)
                except Exception as exc:
                    logger.debug("runner fence stub unavailable: %s", exc)

        try:
            return self.db.build_desktop_session_snapshot(session_id)
        finally:
            if fence:
                fence_release = getattr(self.db, "fence_session", None)
                if callable(fence_release):
                    try:
                        fence_release(session_id, "release")
                    except Exception:
                        pass
                else:
                    try:
                        from agent.direct_desktop_runner import fence_release as _fr

                        _fr(self.db, session_id)
                    except Exception:
                        pass

    # ── frames helpers ───────────────────────────────────────────────────────

    def _prepared_frame_from_row(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        atts = self.db.list_message_attachments(
            row.get("message_id") or "", include_prepared=True
        )
        return {
            "version": "kmanus.hermes.runner.v1",
            "type": "turn.prepared",
            "acceptanceNonce": row["acceptance_nonce"],
            "clientRequestId": row.get("client_request_id"),
            "sessionId": row.get("session_id"),
            "taskId": row.get("task_id"),
            "messageId": row.get("message_id"),
            "attachments": [
                {
                    "attachmentId": a["attachmentId"] if "attachmentId" in a else a.get("attachment_id"),
                    "ordinal": a.get("ordinal", 0),
                    "mimeType": a.get("mimeType") or a.get("mime_type"),
                    "displayName": a.get("displayName") or a.get("display_name"),
                    "previewAvailable": bool(
                        a.get("previewAvailable")
                        if "previewAvailable" in a
                        else a.get("preview_available")
                    ),
                }
                for a in atts
            ],
        }

    def _accepted_frame_from_row(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        frame = self._prepared_frame_from_row(row)
        frame["type"] = "turn.accepted"
        return frame


def _coerce_spec(raw: AttachmentSpec | Mapping[str, Any]) -> AttachmentSpec:
    if isinstance(raw, AttachmentSpec):
        return raw
    data = raw.get("data")
    if data is None:
        raise TurnAcceptanceError("attachment missing data", code="media_persist_failed")
    if isinstance(data, str):
        data = data.encode("utf-8")
    data_b = bytes(data)
    sha = str(raw.get("content_sha256") or raw.get("contentSha256") or "").lower()
    if not sha:
        sha = content_address(data_b)
    return AttachmentSpec(
        attachment_id=str(raw.get("attachment_id") or raw.get("attachmentId")),
        ordinal=int(raw.get("ordinal", 0)),
        mime_type=str(raw.get("mime_type") or raw.get("declaredMimeType") or raw.get("mimeType")),
        display_name=str(raw.get("display_name") or raw.get("displayName") or "attachment"),
        content_sha256=sha,
        data=data_b,
        direction=str(raw.get("direction") or "input"),
    )


def _request_fingerprint(
    *,
    client_request_id: str,
    session_id: str,
    task_id: str,
    message_id: str,
    prompt: str,
    attachments: Sequence[AttachmentSpec],
) -> str:
    h = hashlib.sha256()
    h.update(client_request_id.encode())
    h.update(b"|")
    h.update(session_id.encode())
    h.update(b"|")
    h.update(task_id.encode())
    h.update(b"|")
    h.update(message_id.encode())
    h.update(b"|")
    h.update(prompt.encode())
    for a in sorted(attachments, key=lambda x: x.ordinal):
        h.update(b"|")
        h.update(a.attachment_id.encode())
        h.update(a.content_sha256.encode())
        h.update(str(a.ordinal).encode())
        h.update(a.mime_type.encode())
    return h.hexdigest()


def replay_watermark(session_db: Any, session_id: str) -> int:
    """Helper: max FINALIZED seq after optional fence drain (stubs ok)."""
    native = getattr(session_db, "max_final_seq", None)
    if callable(native):
        try:
            return int(native(session_id))
        except Exception:
            pass
    try:
        from agent.direct_desktop_runner import max_final_seq as _mfs

        return int(_mfs(session_db, session_id))
    except Exception:
        return -1


__all__ = [
    "DirectDesktopMediaService",
    "AttachmentSpec",
    "PreparedTurn",
    "TurnAcceptanceError",
    "STATE_PREPARED",
    "STATE_FINALIZE_INTENT",
    "STATE_COMMITTED",
    "STATE_ABORTED",
    "replay_watermark",
]
