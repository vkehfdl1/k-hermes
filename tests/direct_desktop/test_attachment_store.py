"""Encrypted blob store unit tests: crypto, modes, soft delete, export leases."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from agent.direct_desktop_store import (
    EXPORT_LEASE_TTL_SECONDS,
    FORMAT_VERSION,
    PROFILE_QUOTA_BYTES,
    SESSION_QUOTA_BYTES,
    DirectDesktopMediaStore,
    InMemoryKeyBackend,
    MediaAccessDenied,
    MediaNotFound,
    MediaStoreError,
    QuotaExceeded,
    build_aad,
    content_address,
    hchacha20,
    parse_header,
    xchacha20poly1305_decrypt,
    xchacha20poly1305_encrypt,
)


@pytest.fixture()
def store(tmp_path: Path) -> DirectDesktopMediaStore:
    backend = InMemoryKeyBackend()
    box: dict = {}
    s = DirectDesktopMediaStore(
        tmp_path / "home",
        profile_id="test-profile",
        key_backend=backend,
        open_impl=lambda p: box.__setitem__("opened", str(p)),
        reveal_impl=lambda p: box.__setitem__("revealed", str(p)),
    )
    s._box = box  # type: ignore[attr-defined]
    return s


def test_layout_modes_0700_0600(store: DirectDesktopMediaStore):
    assert store.media_root.is_dir()
    for d in (store.blobs_dir, store.previews_dir, store.trash_dir, store.exports_dir, store.tmp_dir):
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} mode {oct(mode)}"

    prepared = store.write_encrypted_temp(b"hello", mime_type="text/plain")
    final = store.promote_temp(prepared)
    mode = stat.S_IMODE(final.stat().st_mode)
    assert mode == 0o600


def test_xchacha_roundtrip_and_aad_binding(store: DirectDesktopMediaStore):
    pt = b"secret-bytes-" + os.urandom(32)
    mime = "image/png"
    blob_key, packed, header = store.seal(pt, mime_type=mime)
    assert packed.startswith(b"KMHM")
    assert header.format_version == FORMAT_VERSION
    out, hdr = store.open_sealed(packed, blob_key=blob_key)
    assert out == pt
    assert hdr.key_id == store.key_id

    # Tamper AAD field by re-decrypting with wrong blob_key.
    with pytest.raises(Exception):
        store.open_sealed(packed, blob_key="0" * 64)


def test_authenticated_keyid_in_header(store: DirectDesktopMediaStore):
    pt = b"payload"
    blob_key, packed, header = store.seal(pt, mime_type="application/octet-stream")
    h, offset = parse_header(packed)
    assert h.key_id == store.key_id
    assert h.key_version == store.key_version
    assert header.nonce == h.nonce
    # header authenticates via AEAD AAD; body starts after header
    assert offset < len(packed)


def test_promote_is_idempotent(store: DirectDesktopMediaStore):
    prepared = store.write_encrypted_temp(b"abc", mime_type="text/plain")
    p1 = store.promote_temp(prepared)
    # second promote with lasting temp gone should still succeed as existing final
    prepared.temp_path = store.tmp_dir / "ghost.part"
    p2 = store.promote_temp(prepared)
    assert p1 == p2
    assert store.read_final(prepared.blob_key) == b"abc"


def test_soft_delete_and_undelete(store: DirectDesktopMediaStore):
    prepared = store.write_encrypted_temp(b"keep", mime_type="text/plain")
    store.promote_temp(prepared)
    key = prepared.blob_key
    store.soft_delete(key)
    with pytest.raises(MediaNotFound):
        store.read_final(key)
    store.undelete(key)
    assert store.read_final(key) == b"keep"


def test_trash_gc_30_day(store: DirectDesktopMediaStore):
    prepared = store.write_encrypted_temp(b"old", mime_type="text/plain")
    store.promote_temp(prepared)
    key = prepared.blob_key
    store.soft_delete(key)
    trash = store._trash_path(key)
    assert trash.exists()
    # Backdate mtime + meta
    old = time.time() - (31 * 24 * 60 * 60)
    meta = trash.parent / (trash.name + ".meta.json")
    meta.write_text('{"deletedAt": %s, "blobKey": "%s", "kind": "blob"}' % (old, key))
    os.utime(trash, (old, old))
    purged = store.gc_trash(now=time.time())
    assert purged >= 1
    assert not trash.exists()


def test_session_and_profile_quota(store: DirectDesktopMediaStore, tmp_path: Path):
    tiny = DirectDesktopMediaStore(
        tmp_path / "q",
        profile_id="q",
        key_backend=InMemoryKeyBackend(),
        profile_quota=2000,
        session_quota=800,
    )
    sid = "sess-1"
    data = b"x" * 500
    tiny.write_encrypted_temp(data, mime_type="text/plain", session_id=sid)
    # second rewrite that would push session over after accounting promotion size
    # ciphertext > plaintext due to header+tag; 2 big items should exceed session.
    with pytest.raises(QuotaExceeded):
        for _ in range(5):
            tiny.write_encrypted_temp(b"y" * 500, mime_type="text/plain", session_id=sid)


def test_export_lease_never_returns_path_and_open(store: DirectDesktopMediaStore):
    prepared = store.write_encrypted_temp(b"file-body", mime_type="text/plain", blob_key=None)
    store.promote_temp(prepared)
    lease = store.create_export_lease(
        prepared.blob_key,
        display_name="report.png",
        mime_type="image/png",
    )
    assert set(lease.keys()) == {"leaseId", "attachmentDisplayName", "mimeType", "expiresAt"}
    assert "path" not in lease
    assert "/" not in lease["leaseId"]
    # Exclusive dir
    lease_dir = store.exports_dir / lease["leaseId"]
    assert lease_dir.is_dir()
    assert stat.S_IMODE(lease_dir.stat().st_mode) == 0o700

    store.native_open(lease["leaseId"])
    assert store._box.get("opened")  # type: ignore[attr-defined]
    store.native_reveal(lease["leaseId"])
    assert store._box.get("revealed")  # type: ignore[attr-defined]

    # expired lease denied
    meta = store._export_meta[lease["leaseId"]]
    meta["expiresAt"] = time.time() - 1
    with pytest.raises(MediaAccessDenied):
        store.native_open(lease["leaseId"])


def test_export_cleanup_and_ttl(store: DirectDesktopMediaStore):
    prepared = store.write_encrypted_temp(b"z", mime_type="text/plain")
    store.promote_temp(prepared)
    lease = store.create_export_lease(
        prepared.blob_key, display_name="a.bin", mime_type="application/octet-stream"
    )
    # Force expiry
    meta_path = store.exports_dir / lease["leaseId"] / ".lease.json"
    import json

    wire = json.loads(meta_path.read_text())
    wire["expiresAt"] = time.time() - 10
    meta_path.write_text(json.dumps(wire))
    removed = store.cleanup_expired_exports(now=time.time())
    assert removed >= 1
    assert not (store.exports_dir / lease["leaseId"]).exists()


def test_hchacha_stable_and_xchacha_encrypt_api():
    key = bytes(range(32))
    nonce16 = bytes(range(16))
    sub1 = hchacha20(key, nonce16)
    sub2 = hchacha20(key, nonce16)
    assert sub1 == sub2 and len(sub1) == 32
    n24 = os.urandom(24)
    aad = build_aad(
        format_version=1,
        profile_id="p",
        blob_key="k",
        key_id="default",
        key_version=1,
        mime_type="text/plain",
        plaintext_size=5,
    )
    ct = xchacha20poly1305_encrypt(key, n24, b"hello", aad)
    assert xchacha20poly1305_decrypt(key, n24, ct, aad) == b"hello"


def test_content_address_sha256():
    assert content_address(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_inmemory_key_backend_no_plaintext_files(store: DirectDesktopMediaStore):
    # Keys never written under media/
    for p in store.media_root.rglob("*"):
        if p.is_file():
            assert "key" not in p.name.lower() or p.suffix in {".part", ""}


def test_quota_constants():
    assert PROFILE_QUOTA_BYTES == 1 * 1024 * 1024 * 1024
    assert SESSION_QUOTA_BYTES == 250 * 1024 * 1024
    assert EXPORT_LEASE_TTL_SECONDS == 15 * 60
