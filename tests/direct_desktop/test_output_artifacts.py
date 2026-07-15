"""attachment_output_callback and flush attach-intent helpers."""

from __future__ import annotations

from types import SimpleNamespace

from agent.direct_desktop_runner import (
    MEDIA_OUTPUT_CREATED,
    attachment_output_callback,
    flush_pending_attach_intents,
    ingest_attachments,
    queue_attach_intent,
)


class TestOutputCallback:
    def test_emits_media_output_created(self):
        frames = []
        record = {
            "sessionId": "018f0aaa-0000-7000-8000-000000000001",
            "taskId": "018f0aaa-0000-7000-8000-000000000002",
            "messageId": "018f0aaa-0000-7000-8000-000000000003",
            "attachmentId": "018f0aaa-0000-7000-8000-000000000004",
            "mimeType": "image/png",
            "displayName": "plot.png",
            "ordinal": 0,
            "previewAvailable": True,
        }
        frame = attachment_output_callback(record, emit=frames.append)
        assert frame["type"] == MEDIA_OUTPUT_CREATED
        assert frame["direction"] == "output"
        assert frame["attachmentId"] == record["attachmentId"]
        assert frame["mimeType"] == "image/png"
        # metadata only — no path/bytes
        assert "path" not in frame
        assert "bytes" not in frame
        assert "data" not in frame
        assert frames == [frame]

    def test_queue_and_flush(self):
        agent = SimpleNamespace()
        collected = []
        agent._direct_desktop_output_emit = collected.append
        queue_attach_intent(
            agent,
            {
                "sessionId": "018f0aaa-0000-7000-8000-000000000001",
                "attachmentId": "018f0aaa-0000-7000-8000-0000000000aa",
                "mimeType": "image/png",
                "displayName": "a.png",
            },
        )
        queue_attach_intent(
            agent,
            {
                "sessionId": "018f0aaa-0000-7000-8000-000000000001",
                "attachmentId": "018f0aaa-0000-7000-8000-0000000000bb",
                "mimeType": "application/pdf",
                "displayName": "b.pdf",
            },
        )
        drained = flush_pending_attach_intents(agent)
        assert len(drained) == 2
        assert len(collected) == 2
        assert agent._pending_direct_desktop_outputs == []
        # Second flush is empty.
        assert flush_pending_attach_intents(agent) == []


class TestIngestAttachments:
    def test_metadata_only_without_store(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        briefs = [
            {
                "attachmentId": "018f0aaa-0000-7000-8000-0000000000aa",
                "ordinal": 0,
                "declaredMimeType": "image/png",
                "displayName": "scan.png",
                "contentSha256": sha,
                "byteSize": len(data),
                "stagingHandle": "stage-1",
            }
        ]
        records = ingest_attachments(
            briefs,
            resolve_staging=lambda h: data if h == "stage-1" else None,
            store=None,
        )
        assert len(records) == 1
        rec = records[0]
        assert rec.attachment_id.endswith("aa")
        assert rec.mime_type == "image/png"
        assert rec.preview_available is True
        assert rec.state == "PREPARED"
        # store=None falls back to DirectDesktopMediaStore.open_default when available,
        # which materializes a content-addressed store_key from staged bytes.
        assert rec.store_key is not None
        assert len(rec.store_key) >= 16

    def test_store_ingest_called(self):
        class FakeStore:
            def __init__(self):
                self.calls = []

            def ingest(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "storeKey": "blob:" + kwargs["attachment_id"],
                    "previewAvailable": True,
                    "state": "PREPARED",
                }

        data = b"hello-world-bytes-xx"
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        store = FakeStore()
        briefs = [
            {
                "attachmentId": "018f0aaa-0000-7000-8000-0000000000aa",
                "ordinal": 0,
                "declaredMimeType": "text/plain",
                "displayName": "n.txt",
                "contentSha256": sha,
                "byteSize": len(data),
                "stagingHandle": "h",
            }
        ]
        records = ingest_attachments(
            briefs,
            resolve_staging=lambda h: data,
            store=store,
            session_id="s1",
            message_id="m1",
        )
        assert len(store.calls) == 1
        assert records[0].store_key == "blob:018f0aaa-0000-7000-8000-0000000000aa"
