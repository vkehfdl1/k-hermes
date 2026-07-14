"""Encrypted-at-rest direct-desktop media blob store (issue #50).

Layout under ``$HERMES_HOME/media``::

    blobs/      content-addressed encrypted final objects (0600)
    previews/   encrypted preview objects
    trash/      soft-deleted finals pending GC
    exports/    exclusive, short-lived decrypted open/reveal leases
    tmp/        fsync-first promote staging for the 2PC journal

Objects use a ``KMHM`` versioned header followed by XChaCha20-Poly1305
ciphertext (random 24-byte nonce). AAD binds
``{formatVersion, profileId, blobKey, keyId, keyVersion, mimeType, plaintextSize}``
so ciphertext/key/header as a set must agree.

Profile keys live only in the OS keychain abstraction (never plaintext files
or env); tests inject :class:`InMemoryKeyBackend`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
HEADER_MAGIC = b"KMHM"
HEADER_FIXED_LEN = 40  # magic(4)+fmt(1)+flags(1)+keyIdLen(1)+keyVer(4)+nonce(24)+mimeLen(1)+...
DIR_MODE = 0o700
FILE_MODE = 0o600

PROFILE_QUOTA_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB
SESSION_QUOTA_BYTES = 250 * 1024 * 1024  # 250 MiB
TRASH_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 30 days
EXPORT_LEASE_TTL_SECONDS = 15 * 60  # 15 minutes

KEYCHAIN_SERVICE = "k-hermes.direct-desktop.media"
DEFAULT_KEY_ID = "default"


class MediaStoreError(Exception):
    """Base store error."""

    code = "media_persist_failed"

    def __init__(self, message: str = "", *, code: Optional[str] = None):
        super().__init__(message or self.code)
        if code:
            self.code = code


class QuotaExceeded(MediaStoreError):
    code = "media_quota_exceeded"


class MediaNotFound(MediaStoreError):
    code = "media_not_found"


class MediaAccessDenied(MediaStoreError):
    code = "media_access_denied"


class MediaOpenFailed(MediaStoreError):
    code = "media_open_failed"


class KeyBackend(Protocol):
    """Profile OS keychain abstraction."""

    def get(self, profile_id: str, key_id: str) -> Optional[bytes]:
        ...

    def set(self, profile_id: str, key_id: str, key: bytes) -> None:
        ...

    def delete(self, profile_id: str, key_id: str) -> None:
        ...

    def list_key_ids(self, profile_id: str) -> Sequence[str]:
        ...


class InMemoryKeyBackend:
    """Process-local key backend for unit tests (not durable)."""

    def __init__(self) -> None:
        self._keys: Dict[Tuple[str, str], bytes] = {}
        self._lock = threading.Lock()

    def get(self, profile_id: str, key_id: str) -> Optional[bytes]:
        with self._lock:
            value = self._keys.get((profile_id, key_id))
            return bytes(value) if value is not None else None

    def set(self, profile_id: str, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("media profile key must be 32 bytes")
        with self._lock:
            self._keys[(profile_id, key_id)] = bytes(key)

    def delete(self, profile_id: str, key_id: str) -> None:
        with self._lock:
            self._keys.pop((profile_id, key_id), None)

    def list_key_ids(self, profile_id: str) -> Sequence[str]:
        with self._lock:
            return sorted(k for (p, k) in self._keys if p == profile_id)


class OSKeychainBackend:
    """Best-effort OS keychain using the optional ``keyring`` package.

    Falls back to fail-closed (no create-from-call path that would leave a
    plaintext key) when the package or backend is unavailable. Production
    profilers are expected to provision the profile key via setup; tests use
    :class:`InMemoryKeyBackend`.
    """

    def get(self, profile_id: str, key_id: str) -> Optional[bytes]:
        try:
            import keyring  # type: ignore
        except Exception:
            return None
        try:
            raw = keyring.get_password(KEYCHAIN_SERVICE, f"{profile_id}:{key_id}")
        except Exception as exc:  # pragma: no cover
            logger.debug("keychain get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None

    def set(self, profile_id: str, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("media profile key must be 32 bytes")
        try:
            import keyring  # type: ignore
        except Exception as exc:
            raise MediaStoreError(
                "OS keychain unavailable", code="media_persist_failed"
            ) from exc
        keyring.set_password(KEYCHAIN_SERVICE, f"{profile_id}:{key_id}", key.hex())

    def delete(self, profile_id: str, key_id: str) -> None:
        try:
            import keyring  # type: ignore
        except Exception:
            return
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, f"{profile_id}:{key_id}")
        except Exception:
            return

    def list_key_ids(self, profile_id: str) -> Sequence[str]:
        # keyring has no portable list; return empty.
        return ()


# ── XChaCha20-Poly1305 (HChaCha20 + ChaCha20-Poly1305-IETF) ──────────────────


def _rotl32(v: int, c: int) -> int:
    return ((v << c) | (v >> (32 - c))) & 0xFFFFFFFF


def _quarterround(x: list, a: int, b: int, c: int, d: int) -> None:
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF
    x[d] = _rotl32(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF
    x[b] = _rotl32(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF
    x[d] = _rotl32(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF
    x[b] = _rotl32(x[b] ^ x[c], 7)


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    """RFC7539-compatible HChaCha20 (20 rounds, no final add)."""
    if len(key) != 32 or len(nonce16) != 16:
        raise ValueError("HChaCha20 requires 32-byte key and 16-byte nonce")
    x = [
        0x61707865,
        0x3320646E,
        0x79622D32,
        0x6B206574,
        *struct.unpack("<8I", key),
        *struct.unpack("<4I", nonce16),
    ]
    for _ in range(10):
        _quarterround(x, 0, 4, 8, 12)
        _quarterround(x, 1, 5, 9, 13)
        _quarterround(x, 2, 6, 10, 14)
        _quarterround(x, 3, 7, 11, 15)
        _quarterround(x, 0, 5, 10, 15)
        _quarterround(x, 1, 6, 11, 12)
        _quarterround(x, 2, 7, 8, 13)
        _quarterround(x, 3, 4, 9, 14)
    return struct.pack("<8I", x[0], x[1], x[2], x[3], x[12], x[13], x[14], x[15])


def xchacha20poly1305_encrypt(
    key: bytes, nonce24: bytes, plaintext: bytes, aad: bytes
) -> bytes:
    if len(key) != 32 or len(nonce24) != 24:
        raise ValueError("XChaCha20-Poly1305 requires 32-byte key and 24-byte nonce")
    subkey = hchacha20(key, nonce24[:16])
    ietf_nonce = b"\x00\x00\x00\x00" + nonce24[16:]
    return ChaCha20Poly1305(subkey).encrypt(ietf_nonce, plaintext, aad)


def xchacha20poly1305_decrypt(
    key: bytes, nonce24: bytes, ciphertext: bytes, aad: bytes
) -> bytes:
    if len(key) != 32 or len(nonce24) != 24:
        raise ValueError("XChaCha20-Poly1305 requires 32-byte key and 24-byte nonce")
    subkey = hchacha20(key, nonce24[:16])
    ietf_nonce = b"\x00\x00\x00\x00" + nonce24[16:]
    return ChaCha20Poly1305(subkey).decrypt(ietf_nonce, ciphertext, aad)


# ── Header codec ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlobHeader:
    format_version: int
    key_id: str
    key_version: int
    nonce: bytes
    mime_type: str
    plaintext_size: int

    def pack(self) -> bytes:
        key_id_b = self.key_id.encode("utf-8")
        mime_b = self.mime_type.encode("utf-8")
        if not (1 <= len(key_id_b) <= 64):
            raise ValueError("key_id length out of bounds")
        if not (1 <= len(mime_b) <= 128):
            raise ValueError("mime_type length out of bounds")
        if len(self.nonce) != 24:
            raise ValueError("nonce must be 24 bytes")
        if not (0 <= self.key_version <= 0xFFFFFFFF):
            raise ValueError("key_version out of bounds")
        if self.plaintext_size < 0:
            raise ValueError("plaintext_size must be non-negative")
        # layout:
        # magic(4) fmt(1) reserved(1) keyIdLen(1) keyVer(u32be) nonce(24)
        # mimeLen(u16be) ptSize(u64be) keyId mime
        return (
            HEADER_MAGIC
            + bytes([self.format_version & 0xFF, 0, len(key_id_b)])
            + struct.pack(">I", self.key_version)
            + self.nonce
            + struct.pack(">HQ", len(mime_b), self.plaintext_size)
            + key_id_b
            + mime_b
        )


def parse_header(blob: bytes) -> Tuple[BlobHeader, int]:
    if len(blob) < 4 + 1 + 1 + 1 + 4 + 24 + 2 + 8:
        raise MediaStoreError("truncated KMHM header", code="media_persist_failed")
    if blob[0:4] != HEADER_MAGIC:
        raise MediaStoreError("not a KMHM blob", code="media_persist_failed")
    fmt = blob[4]
    key_id_len = blob[6]
    key_version = struct.unpack_from(">I", blob, 7)[0]
    nonce = blob[11:35]
    mime_len, pt_size = struct.unpack_from(">HQ", blob, 35)
    fixed_end = 45
    end = fixed_end + key_id_len + mime_len
    if end > len(blob):
        raise MediaStoreError("truncated KMHM header body", code="media_persist_failed")
    key_id = blob[fixed_end : fixed_end + key_id_len].decode("utf-8")
    mime = blob[fixed_end + key_id_len : end].decode("utf-8")
    header = BlobHeader(
        format_version=fmt,
        key_id=key_id,
        key_version=key_version,
        nonce=nonce,
        mime_type=mime,
        plaintext_size=int(pt_size),
    )
    return header, end


def build_aad(
    *,
    format_version: int,
    profile_id: str,
    blob_key: str,
    key_id: str,
    key_version: int,
    mime_type: str,
    plaintext_size: int,
) -> bytes:
    """Canonical AAD: length-delimited UTF-8 fields (deterministic)."""
    parts = [
        str(int(format_version)),
        profile_id,
        blob_key,
        key_id,
        str(int(key_version)),
        mime_type,
        str(int(plaintext_size)),
    ]
    out = bytearray()
    for p in parts:
        b = p.encode("utf-8")
        out.extend(struct.pack(">H", len(b)))
        out.extend(b)
    return bytes(out)


def content_address(plaintext: bytes) -> str:
    return hashlib.sha256(plaintext).hexdigest()


# ── Filesystem helpers ───────────────────────────────────────────────────────


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, FILE_MODE)
    try:
        # Enforce 0600 even when umask interfered with open mode.
        try:
            os.fchmod(fd, FILE_MODE)
        except OSError:
            pass
        remaining = memoryview(data)
        while remaining:
            n = os.write(fd, remaining)
            remaining = remaining[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_rename(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))
    _fsync_dir(dst.parent)


def _safe_display_name(name: str) -> str:
    base = Path(str(name or "attachment")).name
    # NFC-normalization is the caller's job for wire IDs; scrub controls and path seps.
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in "/\\:\x00")
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = "attachment"
    return cleaned[:180]


# ── Store ────────────────────────────────────────────────────────────────────


@dataclass
class PreparedObject:
    blob_key: str
    temp_path: Path
    ciphertext_size: int
    key_id: str
    key_version: int
    mime_type: str
    plaintext_size: int
    content_sha256: str
    kind: str = "blob"  # blob | preview


class DirectDesktopMediaStore:
    """Profile-private encrypted media object store."""

    def __init__(
        self,
        hermes_home: Path,
        *,
        profile_id: str = "default",
        key_backend: Optional[KeyBackend] = None,
        key_id: str = DEFAULT_KEY_ID,
        key_version: int = 1,
        profile_quota: int = PROFILE_QUOTA_BYTES,
        session_quota: int = SESSION_QUOTA_BYTES,
        open_impl: Optional[Any] = None,
        reveal_impl: Optional[Any] = None,
    ) -> None:
        self.hermes_home = Path(hermes_home)
        self.profile_id = profile_id
        self.key_backend: KeyBackend = key_backend or InMemoryKeyBackend()
        self.key_id = key_id
        self.key_version = int(key_version)
        self.profile_quota = int(profile_quota)
        self.session_quota = int(session_quota)
        self._open_impl = open_impl
        self._reveal_impl = reveal_impl
        self._lock = threading.RLock()
        # session_id -> live bytes accounting (COMMITTED non-trashed)
        self._session_usage: Dict[str, int] = {}
        self._export_meta: Dict[str, Dict[str, Any]] = {}

        self.media_root = self.hermes_home / "media"
        self.blobs_dir = self.media_root / "blobs"
        self.previews_dir = self.media_root / "previews"
        self.trash_dir = self.media_root / "trash"
        self.exports_dir = self.media_root / "exports"
        self.tmp_dir = self.media_root / "tmp"
        self._ensure_layout()
        self._ensure_profile_key()

    # ── construction helpers ─────────────────────────────────────────────────

    @classmethod
    def open_default(
        cls,
        hermes_home: Optional[Path] = None,
        *,
        profile_id: Optional[str] = None,
        key_backend: Optional[KeyBackend] = None,
        **kwargs: Any,
    ) -> "DirectDesktopMediaStore":
        if hermes_home is None:
            try:
                from hermes_constants import get_hermes_home

                hermes_home = get_hermes_home()
            except Exception:
                hermes_home = Path.home() / ".hermes"
        if profile_id is None:
            profile_id = os.environ.get("HERMES_PROFILE", "default") or "default"
        if key_backend is None:
            # Prefer OS keychain; fall back to in-memory only in HERMES_TEST.
            if os.environ.get("HERMES_TEST") or os.environ.get("PYTEST_CURRENT_TEST"):
                key_backend = InMemoryKeyBackend()
            else:
                key_backend = OSKeychainBackend()
        return cls(
            Path(hermes_home),
            profile_id=str(profile_id),
            key_backend=key_backend,
            **kwargs,
        )

    def _ensure_layout(self) -> None:
        for d in (
            self.media_root,
            self.blobs_dir,
            self.previews_dir,
            self.trash_dir,
            self.exports_dir,
            self.tmp_dir,
        ):
            _secure_mkdir(d)

    def _ensure_profile_key(self) -> None:
        existing = self.key_backend.get(self.profile_id, self.key_id)
        if existing is not None and len(existing) == 32:
            return
        # Generate only when backend is writable/test; OSKeychain may refuse.
        key = secrets.token_bytes(32)
        try:
            self.key_backend.set(self.profile_id, self.key_id, key)
        except MediaStoreError:
            # Still install into process if backend is InMemory-like later inject.
            if isinstance(self.key_backend, InMemoryKeyBackend):
                self.key_backend.set(self.profile_id, self.key_id, key)
            else:
                raise

    def _get_key(self, key_id: str) -> bytes:
        key = self.key_backend.get(self.profile_id, key_id)
        if key is None or len(key) != 32:
            raise MediaStoreError(
                f"profile media key missing for {key_id}",
                code="media_persist_failed",
            )
        return key

    # ── quotas ───────────────────────────────────────────────────────────────

    def profile_usage_bytes(self) -> int:
        total = 0
        for directory in (self.blobs_dir, self.previews_dir, self.tmp_dir):
            if not directory.is_dir():
                continue
            for p in directory.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        continue
        return total

    def session_usage_bytes(self, session_id: str) -> int:
        return int(self._session_usage.get(session_id, 0))

    def _assert_quota(self, *, session_id: Optional[str], additional: int) -> None:
        if additional < 0:
            return
        if self.profile_usage_bytes() + additional > self.profile_quota:
            raise QuotaExceeded(
                "profile media quota exceeded", code="media_quota_exceeded"
            )
        if session_id:
            used = self.session_usage_bytes(session_id)
            if used + additional > self.session_quota:
                raise QuotaExceeded(
                    "session media quota exceeded", code="media_quota_exceeded"
                )

    def account_session_bytes(self, session_id: str, delta: int) -> None:
        with self._lock:
            cur = self._session_usage.get(session_id, 0) + delta
            if cur <= 0:
                self._session_usage.pop(session_id, None)
            else:
                self._session_usage[session_id] = cur

    # ── encrypt / write ──────────────────────────────────────────────────────

    def seal(
        self,
        plaintext: bytes,
        *,
        mime_type: str,
        blob_key: Optional[str] = None,
        key_id: Optional[str] = None,
        key_version: Optional[int] = None,
    ) -> Tuple[str, bytes, BlobHeader]:
        blob_key = blob_key or content_address(plaintext)
        key_id = key_id or self.key_id
        key_version = int(self.key_version if key_version is None else key_version)
        nonce = secrets.token_bytes(24)
        header = BlobHeader(
            format_version=FORMAT_VERSION,
            key_id=key_id,
            key_version=key_version,
            nonce=nonce,
            mime_type=mime_type,
            plaintext_size=len(plaintext),
        )
        aad = build_aad(
            format_version=FORMAT_VERSION,
            profile_id=self.profile_id,
            blob_key=blob_key,
            key_id=key_id,
            key_version=key_version,
            mime_type=mime_type,
            plaintext_size=len(plaintext),
        )
        key = self._get_key(key_id)
        ct = xchacha20poly1305_encrypt(key, nonce, plaintext, aad)
        packed = header.pack() + ct
        return blob_key, packed, header

    def open_sealed(self, packed: bytes, *, blob_key: str) -> Tuple[bytes, BlobHeader]:
        header, offset = parse_header(packed)
        aad = build_aad(
            format_version=header.format_version,
            profile_id=self.profile_id,
            blob_key=blob_key,
            key_id=header.key_id,
            key_version=header.key_version,
            mime_type=header.mime_type,
            plaintext_size=header.plaintext_size,
        )
        key = self._get_key(header.key_id)
        pt = xchacha20poly1305_decrypt(key, header.nonce, packed[offset:], aad)
        if len(pt) != header.plaintext_size:
            raise MediaStoreError("plaintext size mismatch", code="media_persist_failed")
        return pt, header

    def write_encrypted_temp(
        self,
        plaintext: bytes,
        *,
        mime_type: str,
        kind: str = "blob",
        blob_key: Optional[str] = None,
        session_id: Optional[str] = None,
        reserve_quota: bool = True,
    ) -> PreparedObject:
        """Encrypt + write to tmp with fsync. Does not promote to final yet."""
        blob_key = blob_key or content_address(plaintext)
        key, packed, header = self.seal(
            plaintext, mime_type=mime_type, blob_key=blob_key
        )
        if reserve_quota:
            self._assert_quota(session_id=session_id, additional=len(packed))
        tmp_name = f"{blob_key}.{uuid.uuid4().hex}.part"
        tmp_path = self.tmp_dir / tmp_name
        with self._lock:
            _write_private_file(tmp_path, packed)
            _fsync_dir(self.tmp_dir)
        return PreparedObject(
            blob_key=blob_key,
            temp_path=tmp_path,
            ciphertext_size=len(packed),
            key_id=header.key_id,
            key_version=header.key_version,
            mime_type=mime_type,
            plaintext_size=len(plaintext),
            content_sha256=content_address(plaintext),
            kind=kind,
        )

    def promote_temp(self, prepared: PreparedObject) -> Path:
        """Idempotent rename into content-addressed final location."""
        final_dir = self.previews_dir if prepared.kind == "preview" else self.blobs_dir
        final_path = final_dir / prepared.blob_key
        with self._lock:
            if final_path.exists():
                # Existing matching final is success; conflicting length is fatal.
                if final_path.stat().st_size != prepared.ciphertext_size:
                    raise MediaStoreError(
                        f"conflicting final for {prepared.blob_key}",
                        code="media_persist_failed",
                    )
                # Drop temp if still present.
                try:
                    if prepared.temp_path.exists():
                        prepared.temp_path.unlink()
                except OSError:
                    pass
                return final_path
            if not prepared.temp_path.exists():
                raise MediaStoreError(
                    f"temp missing for promote: {prepared.blob_key}",
                    code="media_persist_failed",
                )
            _atomic_rename(prepared.temp_path, final_path)
            try:
                os.chmod(final_path, FILE_MODE)
            except OSError:
                pass
            return final_path

    def abort_temp(self, prepared: PreparedObject) -> None:
        try:
            if prepared.temp_path.exists():
                prepared.temp_path.unlink()
        except OSError:
            pass

    def read_final(self, blob_key: str, *, kind: str = "blob") -> bytes:
        path = self._final_path(blob_key, kind=kind)
        if not path.is_file() or path.is_symlink():
            # Try trash recovery path is explicit undelete; soft-deleted
            # objects are not readable as live finals without undelete.
            raise MediaNotFound(f"blob not found: {blob_key}")
        packed = path.read_bytes()
        pt, _ = self.open_sealed(packed, blob_key=blob_key)
        return pt

    def _final_path(self, blob_key: str, *, kind: str = "blob") -> Path:
        base = self.previews_dir if kind == "preview" else self.blobs_dir
        return base / blob_key

    def _trash_path(self, blob_key: str, *, kind: str = "blob") -> Path:
        return self.trash_dir / f"{kind}:{blob_key}"

    # ── soft delete / GC ─────────────────────────────────────────────────────

    def soft_delete(self, blob_key: str, *, kind: str = "blob") -> None:
        src = self._final_path(blob_key, kind=kind)
        if not src.exists():
            raise MediaNotFound(f"blob not found: {blob_key}")
        dst = self._trash_path(blob_key, kind=kind)
        meta = {
            "deletedAt": time.time(),
            "blobKey": blob_key,
            "kind": kind,
        }
        with self._lock:
            _atomic_rename(src, dst)
            _write_private_file(
                Path(str(dst) + ".meta.json"),
                json.dumps(meta, separators=(",", ":")).encode("utf-8"),
            )

    def undelete(self, blob_key: str, *, kind: str = "blob") -> None:
        src = self._trash_path(blob_key, kind=kind)
        if not src.exists():
            raise MediaNotFound(f"trashed blob not found: {blob_key}")
        dst = self._final_path(blob_key, kind=kind)
        with self._lock:
            if dst.exists():
                raise MediaStoreError(
                    "cannot undelete over live final", code="media_persist_failed"
                )
            _atomic_rename(src, dst)
            meta = Path(str(src) + ".meta.json")
            # After rename, meta still sits at original name.
            if meta.exists():
                try:
                    meta.unlink()
                except OSError:
                    pass
            # Meta may have been renamed if we didn't move it; clean dangling.
            dangling = Path(str(dst) + ".meta.json")
            if dangling.exists():
                try:
                    dangling.unlink()
                except OSError:
                    pass

    def gc_trash(self, *, now: Optional[float] = None) -> int:
        """Purge trash entries older than 30 days. Returns number purged."""
        now = time.time() if now is None else now
        purged = 0
        with self._lock:
            for p in list(self.trash_dir.iterdir()):
                if p.name.endswith(".meta.json") or not p.is_file():
                    continue
                meta_path = Path(str(p) + ".meta.json")
                deleted_at = None
                if meta_path.is_file():
                    try:
                        deleted_at = float(json.loads(meta_path.read_text()).get("deletedAt"))
                    except Exception:
                        deleted_at = None
                if deleted_at is None:
                    try:
                        deleted_at = p.stat().st_mtime
                    except OSError:
                        continue
                if now - deleted_at < TRASH_RETENTION_SECONDS:
                    continue
                try:
                    p.unlink()
                    purged += 1
                except OSError:
                    continue
                try:
                    if meta_path.exists():
                        meta_path.unlink()
                except OSError:
                    pass
        return purged

    # ── export leases / open / reveal ────────────────────────────────────────

    def create_export_lease(
        self,
        blob_key: str,
        *,
        display_name: str,
        mime_type: str,
        kind: str = "blob",
        lease_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Decrypt into exclusive exports/<leaseId>/ for native open/reveal.

        Never returns path strings to callers of high-level open/reveal —
        only ``leaseId`` and non-path metadata. Internal methods used by
        open/reveal keep the Path private.
        """
        now = time.time() if now is None else now
        lease_id = lease_id or uuid.uuid4().hex
        lease_dir = self.exports_dir / lease_id
        with self._lock:
            try:
                os.mkdir(lease_dir, DIR_MODE)
            except FileExistsError as exc:
                raise MediaStoreError(
                    "export lease id collision", code="media_open_failed"
                ) from exc
            try:
                os.chmod(lease_dir, DIR_MODE)
            except OSError:
                pass

            try:
                plaintext = self.read_final(blob_key, kind=kind)
            except Exception:
                try:
                    shutil.rmtree(lease_dir, ignore_errors=True)
                except Exception:
                    pass
                raise

            safe_name = _safe_display_name(display_name)
            out_path = lease_dir / safe_name
            _write_private_file(out_path, plaintext)
            _fsync_dir(lease_dir)
            meta = {
                "leaseId": lease_id,
                "blobKey": blob_key,
                "kind": kind,
                "mimeType": mime_type,
                "displayName": safe_name,
                "createdAt": now,
                "expiresAt": now + EXPORT_LEASE_TTL_SECONDS,
                # Internal only — never serialized on the wire:
                "_path": str(out_path),
                "_dir": str(lease_dir),
            }
            self._export_meta[lease_id] = meta
            _write_private_file(
                lease_dir / ".lease.json",
                json.dumps(
                    {k: v for k, v in meta.items() if not k.startswith("_")},
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            return {
                "leaseId": lease_id,
                "attachmentDisplayName": safe_name,
                "mimeType": mime_type,
                "expiresAt": meta["expiresAt"],
            }

    def _resolve_lease_path(self, lease_id: str) -> Path:
        meta = self._export_meta.get(lease_id)
        if meta is None:
            lease_dir = self.exports_dir / lease_id
            lease_file = lease_dir / ".lease.json"
            if not lease_file.is_file():
                raise MediaNotFound(f"export lease not found: {lease_id}")
            try:
                wire = json.loads(lease_file.read_text())
            except Exception as exc:
                raise MediaNotFound(f"export lease corrupt: {lease_id}") from exc
            # Reconstruct internal path only inside k-hermes.
            path = lease_dir / _safe_display_name(wire.get("displayName") or "attachment")
            meta = {
                **wire,
                "_path": str(path),
                "_dir": str(lease_dir),
            }
            self._export_meta[lease_id] = meta
        expires = float(meta.get("expiresAt") or 0)
        if expires and time.time() > expires:
            self.cleanup_export_lease(lease_id)
            raise MediaAccessDenied("export lease expired")
        path = Path(meta["_path"])
        if not path.is_file() or path.is_symlink():
            raise MediaNotFound("export lease file missing")
        # Containment: must live under exports/<leaseId>/
        try:
            path.resolve().relative_to((self.exports_dir / lease_id).resolve())
        except Exception as exc:
            raise MediaAccessDenied("export path escape") from exc
        return path

    def cleanup_export_lease(self, lease_id: str) -> None:
        with self._lock:
            self._export_meta.pop(lease_id, None)
            lease_dir = self.exports_dir / lease_id
            if lease_dir.exists():
                shutil.rmtree(lease_dir, ignore_errors=True)

    def cleanup_expired_exports(self, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            for child in list(self.exports_dir.iterdir()):
                if not child.is_dir():
                    continue
                lease_id = child.name
                meta_path = child / ".lease.json"
                expires = None
                if meta_path.is_file():
                    try:
                        expires = float(json.loads(meta_path.read_text()).get("expiresAt"))
                    except Exception:
                        expires = None
                if expires is None:
                    try:
                        expires = child.stat().st_mtime + EXPORT_LEASE_TTL_SECONDS
                    except OSError:
                        continue
                if now <= expires:
                    continue
                shutil.rmtree(child, ignore_errors=True)
                self._export_meta.pop(lease_id, None)
                removed += 1
        return removed

    def native_open(self, lease_id: str) -> None:
        path = self._resolve_lease_path(lease_id)
        if self._open_impl is not None:
            self._open_impl(path)
            return
        # Default: OS open; path stays inside this process.
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys_platform_is_darwin():
                os.spawnlp(os.P_NOWAIT, "open", "open", str(path))
            else:
                os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", str(path))
        except Exception as exc:
            raise MediaOpenFailed(str(exc)) from exc

    def native_reveal(self, lease_id: str) -> None:
        path = self._resolve_lease_path(lease_id)
        if self._reveal_impl is not None:
            self._reveal_impl(path)
            return
        try:
            if os.name == "nt":
                os.spawnlp(os.P_NOWAIT, "explorer", "explorer", f"/select,{path}")
            elif sys_platform_is_darwin():
                os.spawnlp(os.P_NOWAIT, "open", "open", "-R", str(path))
            else:
                # Best effort: open containing directory.
                os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", str(path.parent))
        except Exception as exc:
            raise MediaOpenFailed(str(exc)) from exc

    # ── high-level convenience used by runner ingest ─────────────────────────

    def ingest(
        self,
        *,
        attachment_id: str,
        data: bytes,
        mime_type: str,
        display_name: str = "attachment",
        content_sha256: Optional[str] = None,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        ordinal: int = 0,
        seal_preview: bool = True,
    ) -> Dict[str, Any]:
        """Write an encrypted temp object for later 2PC promotion.

        Returns store metadata for the PREPARED phase (not yet COMMITTED).
        """
        expected = (content_sha256 or content_address(data)).lower()
        actual = content_address(data)
        if expected != actual:
            raise MediaStoreError("content sha256 mismatch", code="media_persist_failed")

        prepared = self.write_encrypted_temp(
            data,
            mime_type=mime_type,
            kind="blob",
            blob_key=actual,
            session_id=session_id,
            reserve_quota=True,
        )
        preview_available = False
        preview_key = None
        if seal_preview and mime_type.startswith("image/") and len(data) <= 5_242_880:
            # Store encrypted preview as a sealed copy under previews (same
            # content-address for v1; preview bytes may diverge later).
            try:
                prev = self.write_encrypted_temp(
                    data,
                    mime_type=mime_type,
                    kind="preview",
                    blob_key=actual,
                    session_id=session_id,
                    reserve_quota=True,
                )
                preview_key = prev.blob_key
                preview_available = True
                # Keep prepared preview temp alongside blob; both promoted later.
                prepared_preview = prev
            except QuotaExceeded:
                prepared_preview = None
            except Exception:
                prepared_preview = None
        else:
            prepared_preview = None

        return {
            "attachmentId": attachment_id,
            "storeKey": prepared.blob_key,
            "blobKey": prepared.blob_key,
            "keyId": prepared.key_id,
            "keyVersion": prepared.key_version,
            "mimeType": mime_type,
            "displayName": display_name,
            "contentSha256": actual,
            "byteSize": len(data),
            "ciphertextSize": prepared.ciphertext_size,
            "previewAvailable": preview_available,
            "previewKey": preview_key,
            "sessionId": session_id,
            "messageId": message_id,
            "ordinal": ordinal,
            "state": "PREPARED",
            # Internal handles — direct_desktop_media keeps them:
            "_tempPath": str(prepared.temp_path),
            "_previewTempPath": str(prepared_preview.temp_path) if prepared_preview else None,
            "_prepared": prepared,
            "_preparedPreview": prepared_preview,
        }

    def ingest_blob(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.ingest(*args, **kwargs)

    def prepare_write(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.ingest(*args, **kwargs)

    def key_ref_barrier_count(self, key_id: str, key_version: int) -> int:
        """Count live/trash/tmp objects still referencing a key version.

        Full rotation rekey is out of band; the barrier only proves zero
        references before retirement is allowed.
        """
        count = 0
        for directory in (self.blobs_dir, self.previews_dir, self.trash_dir, self.tmp_dir):
            if not directory.is_dir():
                continue
            for p in directory.rglob("*"):
                if not p.is_file() or p.name.endswith(".meta.json") or p.name.endswith(".lease.json"):
                    continue
                try:
                    data = p.read_bytes()
                    header, _ = parse_header(data)
                except Exception:
                    continue
                if header.key_id == key_id and header.key_version == key_version:
                    count += 1
        # Active export leases also pin the cryptosystem tendency but hold
        # plaintext, not key refs; still counted as rotation barrier via
        # non-zero exports when present.
        count += sum(1 for _ in self.exports_dir.iterdir()) if self.exports_dir.is_dir() else 0
        return count


def sys_platform_is_darwin() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


__all__ = [
    "DirectDesktopMediaStore",
    "InMemoryKeyBackend",
    "OSKeychainBackend",
    "PreparedObject",
    "BlobHeader",
    "MediaStoreError",
    "QuotaExceeded",
    "MediaNotFound",
    "MediaAccessDenied",
    "MediaOpenFailed",
    "hchacha20",
    "xchacha20poly1305_encrypt",
    "xchacha20poly1305_decrypt",
    "build_aad",
    "content_address",
    "parse_header",
    "PROFILE_QUOTA_BYTES",
    "SESSION_QUOTA_BYTES",
    "TRASH_RETENTION_SECONDS",
    "EXPORT_LEASE_TTL_SECONDS",
    "FORMAT_VERSION",
]
