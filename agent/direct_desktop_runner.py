"""Direct-desktop media plane: runner control, route preflight, CAS journal.

k-hermes side of the desktop media contract (issue #50 / G002):

* multimodal route preflight (``resolve_multimodal_route``)
* no-strip policy for direct-desktop media turns
* UNSEQUENCED → FINALIZED event CAS (eventKey idempotency, max_final_seq)
* session fence acquire/drain/release + get_session snapshot with replayWatermark
* dispatch state machine COMMITTED → UNCLAIMED → DISPATCH_CLAIMED → RUNNING → TERMINAL
* attachment ingest seam (delegates to DirectDesktopMediaStore when present)
* attachment_output_callback for ``media.output.created`` frames

The encrypted XChaCha20-Poly1305 blob plane lives in ``direct_desktop_store``
(MediaStore peer). This module does not re-implement crypto; it only consumes
store ingest APIs via a stable duck-typed facade.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

RUNNER_VERSION = "dolshoi.hermes.runner.v1"
MEDIA_PROVIDER_UNSUPPORTED = "media_provider_unsupported"
MEDIA_OUTPUT_CREATED = "media.output.created"

# Durable journal event states.
STATE_UNSEQUENCED = "UNSEQUENCED"
STATE_FINALIZED = "FINALIZED"

# Dispatch lifecycle for post-COMMITTED model work.
class DispatchState(str, Enum):
    COMMITTED = "COMMITTED"
    UNCLAIMED = "UNCLAIMED"
    DISPATCH_CLAIMED = "DISPATCH_CLAIMED"
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"

_DISPATCH_TRANSITIONS = {
    DispatchState.COMMITTED: {DispatchState.UNCLAIMED},
    DispatchState.UNCLAIMED: {DispatchState.DISPATCH_CLAIMED, DispatchState.TERMINAL},
    DispatchState.DISPATCH_CLAIMED: {DispatchState.RUNNING, DispatchState.TERMINAL},
    DispatchState.RUNNING: {DispatchState.TERMINAL},
    DispatchState.TERMINAL: set(),
}

_FENCE_FREE = "free"
_FENCE_ACQUIRED = "acquired"
_FENCE_DRAINED = "drained"

_KIND_IMAGE = "image"
_KIND_VIDEO = "video"
_KIND_FILE = "file"
_KIND_EXTRACTED_FILE = "extracted-file"

_IMAGE_MIME_PREFIXES = ("image/",)
_VIDEO_MIME_PREFIXES = ("video/",)
# Files that must be projected via extraction rather than native pixels.
_FILE_MIME_PREFIXES = (
    "application/pdf",
    "text/",
    "application/json",
    "application/msword",
    "application/vnd.",
)


# ── Route resolution ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MultimodalRoute:
    """A fully capable route that can consume every requested media kind."""

    provider: str
    model: str
    supported_kinds: Tuple[str, ...]
    source: str = "primary"  # primary | fallback | credential_pool


@dataclass(frozen=True)
class MultimodalRouteFailure:
    code: str = MEDIA_PROVIDER_UNSUPPORTED
    message: str = "선택한 모델은 이 첨부 형식을 처리할 수 없어요."
    required_kinds: Tuple[str, ...] = ()
    attempted: Tuple[Tuple[str, str], ...] = ()


RouteResult = Union[MultimodalRoute, MultimodalRouteFailure]


def normalize_required_kinds(required_kinds: Iterable[str]) -> Tuple[str, ...]:
    """Normalize kind tokens to {image, video, file, extracted-file}."""
    out: List[str] = []
    seen = set()
    for raw in required_kinds or ():
        kind = (raw or "").strip().lower()
        if not kind:
            continue
        if kind in ("image", "img", "picture", "photo"):
            kind = _KIND_IMAGE
        elif kind in ("video", "vid"):
            kind = _KIND_VIDEO
        elif kind in ("file", "document", "doc", "pdf"):
            kind = _KIND_FILE
        elif kind in ("extracted-file", "extracted_file", "extracted"):
            kind = _KIND_EXTRACTED_FILE
        if kind not in seen:
            seen.add(kind)
            out.append(kind)
    return tuple(out)


def kinds_from_mime_types(mime_types: Iterable[str]) -> Tuple[str, ...]:
    """Map declared MIME types to route kinds for preflight."""
    kinds: List[str] = []
    seen = set()
    for raw in mime_types or ():
        mime = (raw or "").strip().lower()
        if not mime:
            continue
        if any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES):
            kind = _KIND_IMAGE
        elif any(mime.startswith(p) for p in _VIDEO_MIME_PREFIXES):
            kind = _KIND_VIDEO
        else:
            kind = _KIND_FILE
        if kind not in seen:
            seen.add(kind)
            kinds.append(kind)
    return tuple(kinds)


def _capability_for(provider: str, model: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Best-effort media capability map for a provider/model pair."""
    # Default: unknown models are treated as text+file only for safety.
    caps = {
        _KIND_IMAGE: False,
        _KIND_VIDEO: False,
        _KIND_FILE: True,  # extracted-file projection is host-local, not model-dependent
        _KIND_EXTRACTED_FILE: True,
    }
    try:
        from agent.image_routing import _lookup_supports_vision

        vision = _lookup_supports_vision(provider or "", model or "", cfg)
        if vision is True:
            caps[_KIND_IMAGE] = True
        elif vision is False:
            caps[_KIND_IMAGE] = False
        else:
            # Unknown — do not claim image support; preflight must fail closed.
            caps[_KIND_IMAGE] = False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("vision capability lookup failed: %s", exc)

    try:
        from agent.models_dev import get_model_capabilities

        mc = get_model_capabilities(provider or "", model or "")
        if mc is not None:
            mods = set(getattr(mc, "input_modalities", ()) or ())
            if "image" in mods or getattr(mc, "supports_vision", False):
                caps[_KIND_IMAGE] = True
            if "video" in mods:
                caps[_KIND_VIDEO] = True
            if "pdf" in mods or "file" in mods or getattr(mc, "supports_pdf", lambda: False)():
                caps[_KIND_FILE] = True
                caps[_KIND_EXTRACTED_FILE] = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("models.dev capability lookup failed: %s", exc)

    # Explicit config override: model.supports_vision already handled in
    # _lookup_supports_vision. Video override (rare) from providers map.
    if isinstance(cfg, dict):
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        if isinstance(model_cfg, dict):
            if model_cfg.get("supports_video") is True:
                caps[_KIND_VIDEO] = True
            if model_cfg.get("supports_video") is False:
                caps[_KIND_VIDEO] = False
    return caps


def _route_supports(caps: Mapping[str, bool], required: Sequence[str]) -> bool:
    return all(bool(caps.get(kind, False)) for kind in required)


def resolve_multimodal_route(
    required_kinds: Iterable[str],
    provider: Optional[str],
    model: Optional[str],
    fallback_chain: Optional[Sequence[Mapping[str, Any]]] = None,
    credential_pool: Optional[Sequence[Mapping[str, Any]]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> RouteResult:
    """Return a complete capable route or ``media_provider_unsupported``.

    The resolver is fail-closed: if no primary/fallback/pool candidate can
    consume *every* required kind, the turn must terminal-fail before model
    dispatch and must not fall back to silent text-only.
    """
    required = normalize_required_kinds(required_kinds)
    attempted: List[Tuple[str, str]] = []

    def _try(prov: Optional[str], mod: Optional[str], source: str) -> Optional[MultimodalRoute]:
        p = (prov or "").strip()
        m = (mod or "").strip()
        if not p and not m and required:
            return None
        attempted.append((p, m))
        caps = _capability_for(p, m, cfg)
        if required and not _route_supports(caps, required):
            return None
        supported = tuple(k for k in (_KIND_IMAGE, _KIND_VIDEO, _KIND_FILE, _KIND_EXTRACTED_FILE) if caps.get(k))
        return MultimodalRoute(provider=p, model=m, supported_kinds=supported, source=source)

    # Empty kinds → capable text route is fine.
    if not required:
        return MultimodalRoute(
            provider=(provider or "").strip(),
            model=(model or "").strip(),
            supported_kinds=(_KIND_FILE, _KIND_EXTRACTED_FILE),
            source="primary",
        )

    hit = _try(provider, model, "primary")
    if hit is not None:
        return hit

    for entry in fallback_chain or ():
        if not isinstance(entry, Mapping):
            continue
        hit = _try(entry.get("provider"), entry.get("model"), "fallback")
        if hit is not None:
            return hit

    for entry in credential_pool or ():
        if not isinstance(entry, Mapping):
            continue
        hit = _try(
            entry.get("provider") or provider,
            entry.get("model") or model,
            "credential_pool",
        )
        if hit is not None:
            return hit

    return MultimodalRouteFailure(
        code=MEDIA_PROVIDER_UNSUPPORTED,
        required_kinds=required,
        attempted=tuple(attempted),
    )


def enable_direct_desktop_no_strip(agent: Any) -> None:
    """Mark *agent* so conversation_loop never strip-and-retry image rejections."""
    try:
        agent._direct_desktop_media_no_strip = True
    except Exception:  # pragma: no cover
        pass


def is_direct_desktop_no_strip(agent: Any) -> bool:
    return bool(getattr(agent, "_direct_desktop_media_no_strip", False))


def media_provider_unsupported_result(
    messages: list,
    api_call_count: int,
    *,
    detail: str = "",
) -> Dict[str, Any]:
    """Terminal conversation result for unsupported direct-desktop media."""
    summary = MEDIA_PROVIDER_UNSUPPORTED
    if detail:
        summary = f"{MEDIA_PROVIDER_UNSUPPORTED}: {detail}"
    return {
        "final_response": summary,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": summary,
        "error_code": MEDIA_PROVIDER_UNSUPPORTED,
        "media_provider_unsupported": True,
    }


# ── Event journal (UNSEQUENCED / FINALIZED CAS) ──────────────────────────────


_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS desktop_event_journal (
    event_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER,
    state TEXT NOT NULL,
    payload_json TEXT,
    created_at REAL NOT NULL,
    finalized_at REAL
);
CREATE INDEX IF NOT EXISTS idx_desktop_event_session_state
    ON desktop_event_journal(session_id, state, seq);
CREATE INDEX IF NOT EXISTS idx_desktop_event_session_final
    ON desktop_event_journal(session_id, state, seq)
    WHERE state = 'FINALIZED';

CREATE TABLE IF NOT EXISTS desktop_session_fence (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    acquired_at REAL,
    drained_at REAL,
    released_at REAL
);

CREATE TABLE IF NOT EXISTS desktop_dispatch_state (
    acceptance_nonce TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL,
    terminal_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_desktop_dispatch_session
    ON desktop_dispatch_state(session_id, state);
"""


def _now() -> float:
    return time.time()


def ensure_direct_desktop_schema(conn: sqlite3.Connection) -> None:
    """Create CAS/fence/dispatch tables if missing (idempotent)."""
    conn.executescript(_EVENT_SCHEMA_SQL)
    conn.commit()


def _session_conn(session_db: Any) -> Tuple[Any, sqlite3.Connection]:
    """Return (lock_or_none, connection) for SessionDB or raw Connection."""
    if isinstance(session_db, sqlite3.Connection):
        return None, session_db
    conn = getattr(session_db, "_conn", None)
    if conn is None:
        raise TypeError("session_db must expose _conn or be a sqlite3.Connection")
    lock = getattr(session_db, "_lock", None)
    return lock, conn


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_lock(lock):
    return lock if lock is not None else _NullLock()


def max_final_seq(session_db: Any, session_id: str) -> int:
    """Highest FINALIZED seq for *session_id*, or -1 when none exist."""
    # Prefer peer-landed SessionDB helper when present.
    native = getattr(session_db, "max_final_seq", None)
    if callable(native) and not getattr(native, "_direct_desktop_runner_impl", False):
        try:
            return int(native(session_id))
        except TypeError:
            pass  # wrong signature — fall through
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        row = conn.execute(
            "SELECT MAX(seq) AS m FROM desktop_event_journal "
            "WHERE session_id = ? AND state = ? AND seq IS NOT NULL",
            (session_id, STATE_FINALIZED),
        ).fetchone()
        if row is None:
            return -1
        val = row[0] if not isinstance(row, sqlite3.Row) else row["m"]
        return -1 if val is None else int(val)


max_final_seq._direct_desktop_runner_impl = True  # type: ignore[attr-defined]


def allocate_unsequenced_event(
    session_db: Any,
    session_id: str,
    event_key: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert UNSEQUENCED journal row for *event_key* (idempotent on key).

    Replaying the same event_key returns the existing row without allocating a
    new sequence. FINALIZED rows keep their seq.
    """
    lock, conn = _session_conn(session_db)
    payload_json = json.dumps(dict(payload or {}), separators=(",", ":"), sort_keys=True)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        existing = conn.execute(
            "SELECT event_key, session_id, seq, state, payload_json, created_at, finalized_at "
            "FROM desktop_event_journal WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is not None:
            return _row_to_event(existing)
        created = _now()
        conn.execute(
            "INSERT INTO desktop_event_journal "
            "(event_key, session_id, seq, state, payload_json, created_at, finalized_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL)",
            (event_key, session_id, STATE_UNSEQUENCED, payload_json, created),
        )
        conn.commit()
        return {
            "eventKey": event_key,
            "sessionId": session_id,
            "seq": None,
            "state": STATE_UNSEQUENCED,
            "payload": dict(payload or {}),
            "createdAt": created,
            "finalizedAt": None,
        }


def finalize_event_key(
    session_db: Any,
    session_id: str,
    event_key: str,
    *,
    desired_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """CAS-finalize an UNSEQUENCED event to the next session sequence.

    CAS properties:
      * same event_key + already FINALIZED → return existing finalized row
        (idempotent; seq never changes)
      * different session_id on existing key → conflict error
      * concurrent first-finalize: UNIQUE/primary on event_key plus
        transaction around next-seq selection

    Returns dict with keys eventKey, sessionId, seq, state, createdAt, finalizedAt.
    """
    native = getattr(session_db, "finalize_event_key", None)
    if callable(native) and not getattr(native, "_direct_desktop_runner_impl", False):
        try:
            return native(session_id, event_key, desired_seq=desired_seq)
        except TypeError:
            try:
                return native(session_id, event_key)
            except TypeError:
                pass

    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        # SessionDB opens with isolation_level=None (autocommit). Track whether
        # we opened an IMMEDIATE txn so COMMIT/ROLLBACK are only used then.
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
        except sqlite3.OperationalError:
            began = False

        def _commit() -> None:
            if began:
                try:
                    conn.execute("COMMIT")
                except sqlite3.OperationalError:
                    pass

        def _rollback() -> None:
            if began:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass

        try:
            existing = conn.execute(
                "SELECT event_key, session_id, seq, state, payload_json, created_at, finalized_at "
                "FROM desktop_event_journal WHERE event_key = ?",
                (event_key,),
            ).fetchone()

            if existing is not None:
                row = _row_to_event(existing)
                if row["sessionId"] != session_id:
                    _rollback()
                    raise ValueError(
                        f"event_key session mismatch: key={event_key} "
                        f"have={row['sessionId']} want={session_id}"
                    )
                if row["state"] == STATE_FINALIZED:
                    _commit()
                    return row
                next_seq = (
                    int(desired_seq)
                    if desired_seq is not None
                    else _next_seq_unlocked(conn, session_id)
                )
                finalized_at = _now()
                conn.execute(
                    "UPDATE desktop_event_journal "
                    "SET seq = ?, state = ?, finalized_at = ? "
                    "WHERE event_key = ? AND state = ?",
                    (next_seq, STATE_FINALIZED, finalized_at, event_key, STATE_UNSEQUENCED),
                )
                after = conn.execute(
                    "SELECT event_key, session_id, seq, state, payload_json, created_at, finalized_at "
                    "FROM desktop_event_journal WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
                _commit()
                return _row_to_event(after)

            next_seq = (
                int(desired_seq)
                if desired_seq is not None
                else _next_seq_unlocked(conn, session_id)
            )
            created = _now()
            conn.execute(
                "INSERT INTO desktop_event_journal "
                "(event_key, session_id, seq, state, payload_json, created_at, finalized_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_key, session_id, next_seq, STATE_FINALIZED, "{}", created, created),
            )
            _commit()
            return {
                "eventKey": event_key,
                "sessionId": session_id,
                "seq": next_seq,
                "state": STATE_FINALIZED,
                "payload": {},
                "createdAt": created,
                "finalizedAt": created,
            }
        except Exception:
            _rollback()
            raise


finalize_event_key._direct_desktop_runner_impl = True  # type: ignore[attr-defined]


def _next_seq_unlocked(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(seq) AS m FROM desktop_event_journal "
        "WHERE session_id = ? AND state = ? AND seq IS NOT NULL",
        (session_id, STATE_FINALIZED),
    ).fetchone()
    val = row[0] if row is not None else None
    if isinstance(row, sqlite3.Row):
        val = row["m"]
    return 0 if val is None else int(val) + 1


def _row_to_event(row: Any) -> Dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        data = dict(row)
        keys = {
            "event_key": data.get("event_key"),
            "session_id": data.get("session_id"),
            "seq": data.get("seq"),
            "state": data.get("state"),
            "payload_json": data.get("payload_json"),
            "created_at": data.get("created_at"),
            "finalized_at": data.get("finalized_at"),
        }
    else:
        keys = {
            "event_key": row[0],
            "session_id": row[1],
            "seq": row[2],
            "state": row[3],
            "payload_json": row[4],
            "created_at": row[5],
            "finalized_at": row[6],
        }
    payload: Dict[str, Any] = {}
    raw = keys["payload_json"]
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
    return {
        "eventKey": keys["event_key"],
        "sessionId": keys["session_id"],
        "seq": keys["seq"],
        "state": keys["state"],
        "payload": payload,
        "createdAt": keys["created_at"],
        "finalizedAt": keys["finalized_at"],
    }


def process_pending_sequence_request(
    session_db: Any,
    frame: Mapping[str, Any],
) -> Dict[str, Any]:
    """Handle a ``pending_sequence_request`` control frame.

    Allocates UNSEQUENCED then Finalizes to the next seq, returning a
    ``pending_sequence_finalized`` reply. Idempotent on eventKey.
    """
    event_key = str(frame.get("eventKey") or "")
    session_id = str(frame.get("sessionId") or "")
    if not event_key or not session_id:
        raise ValueError("pending_sequence_request requires eventKey and sessionId")
    # Ensure UNSEQUENCED exists so restart can resume before finalize.
    allocate_unsequenced_event(session_db, session_id, event_key)
    finalized = finalize_event_key(session_db, session_id, event_key)
    return {
        "version": RUNNER_VERSION,
        "type": "pending_sequence_finalized",
        "eventKey": event_key,
        "sessionId": session_id,
        "seq": int(finalized["seq"]),
    }


def process_max_final_seq_request(
    session_db: Any,
    frame: Mapping[str, Any],
) -> Dict[str, Any]:
    session_id = str(frame.get("sessionId") or "")
    if not session_id:
        raise ValueError("max_final_seq_request requires sessionId")
    return {
        "version": RUNNER_VERSION,
        "type": "max_final_seq_result",
        "sessionId": session_id,
        "maxFinalSeq": max_final_seq(session_db, session_id),
    }


def list_unsequenced_events(session_db: Any, session_id: str) -> List[Dict[str, Any]]:
    """Events that must be finalized after restart refile."""
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        rows = conn.execute(
            "SELECT event_key, session_id, seq, state, payload_json, created_at, finalized_at "
            "FROM desktop_event_journal WHERE session_id = ? AND state = ? "
            "ORDER BY created_at ASC",
            (session_id, STATE_UNSEQUENCED),
        ).fetchall()
        return [_row_to_event(r) for r in rows]


def restart_finalize_unsequenced(
    session_db: Any,
    session_id: str,
) -> List[Dict[str, Any]]:
    """Finalize every lingering UNSEQUENCED event after process restart.

    Preserves eventKey identity and assigns monotonic seq. Already FINALIZED
    keys are left untouched (CAS-idempotent).
    """
    pending = list_unsequenced_events(session_db, session_id)
    out: List[Dict[str, Any]] = []
    for ev in pending:
        out.append(finalize_event_key(session_db, session_id, ev["eventKey"]))
    return out


# ── Session fence ────────────────────────────────────────────────────────────


def fence_acquire(session_db: Any, session_id: str) -> Dict[str, Any]:
    """Acquire a session fence (blocks new writers until drained/released)."""
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        now = _now()
        conn.execute(
            "INSERT INTO desktop_session_fence (session_id, status, acquired_at, drained_at, released_at) "
            "VALUES (?, ?, ?, NULL, NULL) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "status = excluded.status, acquired_at = excluded.acquired_at, "
            "drained_at = NULL, released_at = NULL",
            (session_id, _FENCE_ACQUIRED, now),
        )
        conn.commit()
    return {
        "version": RUNNER_VERSION,
        "type": "fence_ack",
        "sessionId": session_id,
        "status": "acquired",
    }


def fence_drain(session_db: Any, session_id: str) -> Dict[str, Any]:
    """Mark fence drained so a snapshot can capture a stable watermark."""
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        now = _now()
        conn.execute(
            "INSERT INTO desktop_session_fence (session_id, status, acquired_at, drained_at, released_at) "
            "VALUES (?, ?, ?, ?, NULL) "
            "ON CONFLICT(session_id) DO UPDATE SET status = ?, drained_at = ?",
            (session_id, _FENCE_DRAINED, now, now, _FENCE_DRAINED, now),
        )
        conn.commit()
    return {
        "version": RUNNER_VERSION,
        "type": "fence_ack",
        "sessionId": session_id,
        "status": "drained",
    }


def fence_release(session_db: Any, session_id: str) -> Dict[str, Any]:
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        now = _now()
        conn.execute(
            "INSERT INTO desktop_session_fence (session_id, status, acquired_at, drained_at, released_at) "
            "VALUES (?, ?, NULL, NULL, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET status = ?, released_at = ?",
            (session_id, _FENCE_FREE, now, _FENCE_FREE, now),
        )
        conn.commit()
    return {
        "version": RUNNER_VERSION,
        "type": "fence_ack",
        "sessionId": session_id,
        "status": "released",
    }


def process_fence_request(session_db: Any, frame: Mapping[str, Any], *, phase: str = "acquire") -> Dict[str, Any]:
    session_id = str(frame.get("sessionId") or "")
    if not session_id:
        raise ValueError("fence_request requires sessionId")
    if phase == "drain":
        return fence_drain(session_db, session_id)
    if phase == "release":
        return fence_release(session_db, session_id)
    return fence_acquire(session_db, session_id)


def get_session_snapshot(
    session_db: Any,
    session_id: str,
    *,
    acquire_fence: bool = True,
) -> Dict[str, Any]:
    """Fence-drain then return session metadata including replayWatermark.

    ``replayWatermark`` equals ``max_final_seq`` after the fence has been
    drained so in-flight UNSEQUENCED writers settle (or are excluded).
    """
    if acquire_fence:
        fence_acquire(session_db, session_id)
        fence_drain(session_db, session_id)
    try:
        base: Dict[str, Any] = {}
        getter = getattr(session_db, "get_session", None)
        if callable(getter):
            row = getter(session_id)
            if isinstance(row, dict):
                base = dict(row)
            elif row is not None:
                base = {"id": session_id, "raw": row}
        else:
            base = {"id": session_id}
        watermark = max_final_seq(session_db, session_id)
        base["replayWatermark"] = watermark
        base["sessionId"] = base.get("sessionId") or base.get("id") or session_id
        return base
    finally:
        if acquire_fence:
            fence_release(session_db, session_id)


# ── Dispatch state machine ───────────────────────────────────────────────────


def set_dispatch_state(
    session_db: Any,
    acceptance_nonce: str,
    session_id: str,
    task_id: str,
    state: Union[str, DispatchState],
    *,
    terminal_reason: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Advance (or seed) dispatch lifecycle with optional transition check."""
    target = DispatchState(state) if not isinstance(state, DispatchState) else state
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        existing = conn.execute(
            "SELECT acceptance_nonce, session_id, task_id, state, updated_at, terminal_reason "
            "FROM desktop_dispatch_state WHERE acceptance_nonce = ?",
            (acceptance_nonce,),
        ).fetchone()
        now = _now()
        if existing is None:
            if target is not DispatchState.COMMITTED and not force:
                # First durable visibility after 2PC is always COMMITTED.
                raise ValueError(f"dispatch must start at COMMITTED, got {target.value}")
            conn.execute(
                "INSERT INTO desktop_dispatch_state "
                "(acceptance_nonce, session_id, task_id, state, updated_at, terminal_reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    acceptance_nonce,
                    session_id,
                    task_id,
                    target.value,
                    now,
                    terminal_reason,
                ),
            )
            conn.commit()
        else:
            current = DispatchState(
                existing["state"] if isinstance(existing, sqlite3.Row) else existing[3]
            )
            if current == target:
                # Idempotent re-entry.
                pass
            elif not force and target not in _DISPATCH_TRANSITIONS.get(current, set()):
                raise ValueError(
                    f"illegal dispatch transition {current.value} → {target.value}"
                )
            else:
                conn.execute(
                    "UPDATE desktop_dispatch_state "
                    "SET state = ?, updated_at = ?, terminal_reason = COALESCE(?, terminal_reason) "
                    "WHERE acceptance_nonce = ?",
                    (target.value, now, terminal_reason, acceptance_nonce),
                )
                conn.commit()
        row = conn.execute(
            "SELECT acceptance_nonce, session_id, task_id, state, updated_at, terminal_reason "
            "FROM desktop_dispatch_state WHERE acceptance_nonce = ?",
            (acceptance_nonce,),
        ).fetchone()
    return _dispatch_row(row)


def get_dispatch_state(session_db: Any, acceptance_nonce: str) -> Optional[Dict[str, Any]]:
    lock, conn = _session_conn(session_db)
    with _with_lock(lock):
        ensure_direct_desktop_schema(conn)
        row = conn.execute(
            "SELECT acceptance_nonce, session_id, task_id, state, updated_at, terminal_reason "
            "FROM desktop_dispatch_state WHERE acceptance_nonce = ?",
            (acceptance_nonce,),
        ).fetchone()
        if row is None:
            return None
        return _dispatch_row(row)


def advance_dispatch_to_running(
    session_db: Any,
    acceptance_nonce: str,
    session_id: str,
    task_id: str,
) -> Dict[str, Any]:
    """Seed COMMITTED if needed and walk UNCLAIMED → CLAIMED → RUNNING."""
    current = get_dispatch_state(session_db, acceptance_nonce)
    if current is None:
        set_dispatch_state(
            session_db, acceptance_nonce, session_id, task_id, DispatchState.COMMITTED
        )
    order = [
        DispatchState.UNCLAIMED,
        DispatchState.DISPATCH_CLAIMED,
        DispatchState.RUNNING,
    ]
    state = get_dispatch_state(session_db, acceptance_nonce)
    assert state is not None
    cur = DispatchState(state["state"])
    for nxt in order:
        if cur == DispatchState.TERMINAL:
            break
        if cur == nxt:
            continue
        # Walk only forward along the legal path.
        if nxt.value not in {s.value for s in _DISPATCH_TRANSITIONS.get(cur, set())} and cur != nxt:
            # Skip if not immediate, try stepwise from current
            pass
        try:
            state = set_dispatch_state(
                session_db, acceptance_nonce, session_id, task_id, nxt
            )
            cur = DispatchState(state["state"])
        except ValueError:
            # Already past this step.
            if DispatchState(state["state"]).value == nxt.value:
                cur = nxt
            else:
                raise
    return state


def terminalize_dispatch(
    session_db: Any,
    acceptance_nonce: str,
    session_id: str,
    task_id: str,
    reason: str = "completed",
) -> Dict[str, Any]:
    return set_dispatch_state(
        session_db,
        acceptance_nonce,
        session_id,
        task_id,
        DispatchState.TERMINAL,
        terminal_reason=reason,
        force=True,
    )


def _dispatch_row(row: Any) -> Dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {
            "acceptanceNonce": row["acceptance_nonce"],
            "sessionId": row["session_id"],
            "taskId": row["task_id"],
            "state": row["state"],
            "updatedAt": row["updated_at"],
            "terminalReason": row["terminal_reason"],
        }
    return {
        "acceptanceNonce": row[0],
        "sessionId": row[1],
        "taskId": row[2],
        "state": row[3],
        "updatedAt": row[4],
        "terminalReason": row[5],
    }


# ── Attachment ingest + output callback ──────────────────────────────────────


@dataclass
class IngestedAttachment:
    attachment_id: str
    ordinal: int
    mime_type: str
    display_name: str
    content_sha256: str
    byte_size: int
    preview_available: bool = False
    store_key: Optional[str] = None
    state: str = "PREPARED"


def ingest_attachments(
    briefs: Sequence[Mapping[str, Any]],
    *,
    resolve_staging: Optional[Callable[[str], bytes]] = None,
    store: Any = None,
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> List[IngestedAttachment]:
    """Validate attachment briefs and ingest into the durable store when present.

    *resolve_staging* maps ``stagingHandle`` → plaintext bytes.
    *store* is a DirectDesktopMediaStore (or duck-typed) exposing
    ``ingest`` / ``ingest_blob`` / ``prepare_write``.
    Without a store the function returns validated metadata-only PREPARED
    records so the 2PC control plane can still be unit-tested.
    """
    if store is None:
        store = _try_default_store()

    out: List[IngestedAttachment] = []
    for brief in briefs or ():
        if not isinstance(brief, Mapping):
            raise ValueError("attachment brief must be an object")
        attachment_id = str(brief.get("attachmentId") or "")
        ordinal = int(brief.get("ordinal", 0))
        mime = str(brief.get("declaredMimeType") or brief.get("mimeType") or "")
        display = str(brief.get("displayName") or "")
        sha = str(brief.get("contentSha256") or "").lower()
        size = int(brief.get("byteSize") or 0)
        handle = str(brief.get("stagingHandle") or "")
        if not attachment_id or not mime or not display or not sha or not handle:
            raise ValueError("attachment brief missing required fields")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("contentSha256 must be lowercase hex SHA-256")

        blob: Optional[bytes] = None
        if resolve_staging is not None:
            blob = resolve_staging(handle)
            if blob is None:
                raise ValueError(f"staging handle unresolved: {handle}")
            if len(blob) != size and size > 0:
                # Fail closed on declared vs observed length.
                raise ValueError("byteSize mismatch for staging handle")

        store_key = None
        preview = False
        state = "PREPARED"
        if store is not None and blob is not None:
            record = _store_ingest(
                store,
                attachment_id=attachment_id,
                mime_type=mime,
                display_name=display,
                content_sha256=sha,
                data=blob,
                session_id=session_id,
                message_id=message_id,
                ordinal=ordinal,
            )
            store_key = record.get("storeKey") or record.get("blobKey") or record.get("key")
            preview = bool(record.get("previewAvailable", False))
            state = str(record.get("state") or state)

        out.append(
            IngestedAttachment(
                attachment_id=attachment_id,
                ordinal=ordinal,
                mime_type=mime,
                display_name=display,
                content_sha256=sha,
                byte_size=size,
                preview_available=preview or mime.startswith("image/"),
                store_key=store_key,
                state=state,
            )
        )
    return out


def _try_default_store() -> Any:
    try:
        from agent.direct_desktop_store import DirectDesktopMediaStore  # type: ignore

        if hasattr(DirectDesktopMediaStore, "open_default"):
            return DirectDesktopMediaStore.open_default()
        return None
    except Exception:
        return None


def _store_ingest(
    store: Any,
    *,
    attachment_id: str,
    mime_type: str,
    display_name: str,
    content_sha256: str,
    data: bytes,
    session_id: Optional[str],
    message_id: Optional[str],
    ordinal: int,
) -> Dict[str, Any]:
    kwargs = {
        "attachment_id": attachment_id,
        "mime_type": mime_type,
        "display_name": display_name,
        "content_sha256": content_sha256,
        "data": data,
        "session_id": session_id,
        "message_id": message_id,
        "ordinal": ordinal,
    }
    for name in ("ingest", "ingest_blob", "prepare_write"):
        fn = getattr(store, name, None)
        if not callable(fn):
            continue
        try:
            result = fn(**kwargs)
        except TypeError:
            # Positional fallback for simpler signatures: (attachment_id, data, **meta)
            try:
                result = fn(attachment_id, data, mime_type=mime_type, display_name=display_name)
            except TypeError:
                continue
        if result is None:
            return {}
        if isinstance(result, dict):
            return result
        return {"storeKey": str(result)}
    logger.debug("store has no ingest/ingest_blob/prepare_write — metadata only")
    return {}


def attachment_output_callback(
    record: Mapping[str, Any],
    *,
    emit: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Emit a metadata-only ``media.output.created`` control/runtime frame.

    Bytes never leave k-hermes on this path; only attachment IDs + MIME +
    display name. ``emit`` defaults to a no-op (tests inject a collector).
    """
    frame = {
        "version": "dolshoi.hermes.runtime-event.v1",
        "type": MEDIA_OUTPUT_CREATED,
        "sessionId": record.get("sessionId") or record.get("session_id"),
        "taskId": record.get("taskId") or record.get("task_id"),
        "messageId": record.get("messageId") or record.get("message_id"),
        "attachmentId": record.get("attachmentId") or record.get("attachment_id"),
        "mimeType": record.get("mimeType") or record.get("mime_type"),
        "displayName": record.get("displayName") or record.get("display_name"),
        "ordinal": record.get("ordinal", 0),
        "previewAvailable": bool(record.get("previewAvailable", record.get("preview_available", False))),
        "direction": "output",
    }
    # Drop None values so additionalProperties-closed consumers accept optional IDs.
    frame = {k: v for k, v in frame.items() if v is not None}
    if emit is not None:
        emit(frame)
    return frame


def flush_pending_attach_intents(agent: Any) -> List[Dict[str, Any]]:
    """Drain ``agent._pending_direct_desktop_outputs`` via output callback.

    Called from run_agent flush paths so assistant/tool artifacts amassed
    during a direct-desktop turn become durable output linkage + host frames
    after the assistant message row is written.
    """
    pending = getattr(agent, "_pending_direct_desktop_outputs", None)
    if not pending:
        return []
    emit = getattr(agent, "_direct_desktop_output_emit", None)
    drained: List[Dict[str, Any]] = []
    # Clear first so recursive flush cannot re-enter the same queue.
    try:
        agent._pending_direct_desktop_outputs = []
    except Exception:
        pass
    for record in pending:
        try:
            drained.append(attachment_output_callback(record, emit=emit))
        except Exception as exc:  # pragma: no cover - defense
            logger.warning("attachment_output_callback failed: %s", exc)
    return drained


def queue_attach_intent(agent: Any, record: Mapping[str, Any]) -> None:
    """Queue an output artifact for the next flush_pending_attach_intents call."""
    pending = getattr(agent, "_pending_direct_desktop_outputs", None)
    if pending is None:
        pending = []
        try:
            agent._pending_direct_desktop_outputs = pending
        except Exception:
            return
    pending.append(dict(record))


# ── Control-frame dispatcher ─────────────────────────────────────────────────


def handle_runner_control(
    session_db: Any,
    frame: Mapping[str, Any],
    *,
    fence_phase: str = "acquire",
) -> Dict[str, Any]:
    """Dispatch a single dolshoi.hermes.runner.v1 control frame."""
    if not isinstance(frame, Mapping):
        raise ValueError("control frame must be an object")
    ftype = frame.get("type") or frame.get("op")
    if ftype == "pending_sequence_request":
        return process_pending_sequence_request(session_db, frame)
    if ftype == "max_final_seq_request":
        return process_max_final_seq_request(session_db, frame)
    if ftype == "fence_request":
        return process_fence_request(session_db, frame, phase=fence_phase)
    raise ValueError(f"unsupported runner control type: {ftype!r}")


# ── Runner facade ────────────────────────────────────────────────────────────


@dataclass
class DirectDesktopRunner:
    """In-process service used by the stdout-JSONL turn entrypoint."""

    session_db: Any
    store: Any = None
    output_emit: Optional[Callable[[Mapping[str, Any]], None]] = None
    cfg: Optional[Dict[str, Any]] = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def resolve_route(
        self,
        required_kinds: Iterable[str],
        provider: Optional[str],
        model: Optional[str],
        fallback_chain: Optional[Sequence[Mapping[str, Any]]] = None,
        credential_pool: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> RouteResult:
        return resolve_multimodal_route(
            required_kinds,
            provider,
            model,
            fallback_chain=fallback_chain,
            credential_pool=credential_pool,
            cfg=self.cfg,
        )

    def ingest(
        self,
        briefs: Sequence[Mapping[str, Any]],
        *,
        resolve_staging: Optional[Callable[[str], bytes]] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> List[IngestedAttachment]:
        return ingest_attachments(
            briefs,
            resolve_staging=resolve_staging,
            store=self.store,
            session_id=session_id,
            message_id=message_id,
        )

    def pending_sequence(self, event_key: str, session_id: str) -> Dict[str, Any]:
        return process_pending_sequence_request(
            self.session_db,
            {
                "version": RUNNER_VERSION,
                "type": "pending_sequence_request",
                "eventKey": event_key,
                "sessionId": session_id,
            },
        )

    def max_final_seq(self, session_id: str) -> int:
        return max_final_seq(self.session_db, session_id)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        return get_session_snapshot(self.session_db, session_id)

    def emit_output(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        return attachment_output_callback(record, emit=self.output_emit)

    def bind_agent_no_strip(self, agent: Any) -> None:
        enable_direct_desktop_no_strip(agent)
        if self.output_emit is not None:
            try:
                agent._direct_desktop_output_emit = self.output_emit
            except Exception:
                pass


__all__ = [
    "RUNNER_VERSION",
    "MEDIA_PROVIDER_UNSUPPORTED",
    "MEDIA_OUTPUT_CREATED",
    "STATE_UNSEQUENCED",
    "STATE_FINALIZED",
    "DispatchState",
    "MultimodalRoute",
    "MultimodalRouteFailure",
    "IngestedAttachment",
    "DirectDesktopRunner",
    "normalize_required_kinds",
    "kinds_from_mime_types",
    "resolve_multimodal_route",
    "enable_direct_desktop_no_strip",
    "is_direct_desktop_no_strip",
    "media_provider_unsupported_result",
    "ensure_direct_desktop_schema",
    "max_final_seq",
    "allocate_unsequenced_event",
    "finalize_event_key",
    "process_pending_sequence_request",
    "process_max_final_seq_request",
    "list_unsequenced_events",
    "restart_finalize_unsequenced",
    "fence_acquire",
    "fence_drain",
    "fence_release",
    "process_fence_request",
    "get_session_snapshot",
    "set_dispatch_state",
    "get_dispatch_state",
    "advance_dispatch_to_running",
    "terminalize_dispatch",
    "ingest_attachments",
    "attachment_output_callback",
    "flush_pending_attach_intents",
    "queue_attach_intent",
    "handle_runner_control",
]
