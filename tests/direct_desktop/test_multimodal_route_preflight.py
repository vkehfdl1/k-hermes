"""Route preflight + direct-desktop no-strip recovery (issue #50)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.direct_desktop_runner import (
    MEDIA_PROVIDER_UNSUPPORTED,
    MultimodalRoute,
    MultimodalRouteFailure,
    enable_direct_desktop_no_strip,
    is_direct_desktop_no_strip,
    kinds_from_mime_types,
    media_provider_unsupported_result,
    resolve_multimodal_route,
)
from agent.message_sanitization import _strip_images_from_messages


class TestKindsFromMime:
    def test_image_video_file(self):
        kinds = kinds_from_mime_types(
            ["image/png", "video/mp4", "application/pdf", "IMAGE/JPEG"]
        )
        assert kinds == ("image", "video", "file")

    def test_empty(self):
        assert kinds_from_mime_types([]) == ()


class TestResolveMultimodalRoute:
    def test_empty_kinds_is_text_ok(self):
        route = resolve_multimodal_route([], "openrouter", "any-text-model")
        assert isinstance(route, MultimodalRoute)
        assert route.provider == "openrouter"

    def test_image_with_vision_capable(self):
        with patch(
            "agent.direct_desktop_runner._capability_for",
            return_value={
                "image": True,
                "video": False,
                "file": True,
                "extracted-file": True,
            },
        ):
            route = resolve_multimodal_route(
                ["image"], "anthropic", "claude-sonnet-4"
            )
        assert isinstance(route, MultimodalRoute)
        assert "image" in route.supported_kinds
        assert route.source == "primary"

    def test_image_with_text_only_fails(self):
        with patch(
            "agent.direct_desktop_runner._capability_for",
            return_value={
                "image": False,
                "video": False,
                "file": True,
                "extracted-file": True,
            },
        ):
            result = resolve_multimodal_route(
                ["image"], "deepseek", "deepseek-v4-pro"
            )
        assert isinstance(result, MultimodalRouteFailure)
        assert result.code == MEDIA_PROVIDER_UNSUPPORTED
        assert "image" in result.required_kinds

    def test_fallback_chain_used(self):
        calls = []

        def fake_caps(provider, model, cfg=None):
            calls.append((provider, model))
            # First (primary) cannot do image; fallback can.
            if model == "vision-model":
                return {
                    "image": True,
                    "video": False,
                    "file": True,
                    "extracted-file": True,
                }
            return {
                "image": False,
                "video": False,
                "file": True,
                "extracted-file": True,
            }

        with patch("agent.direct_desktop_runner._capability_for", side_effect=fake_caps):
            route = resolve_multimodal_route(
                ["image"],
                "primary-prov",
                "text-model",
                fallback_chain=[
                    {"provider": "fb", "model": "vision-model"},
                ],
            )
        assert isinstance(route, MultimodalRoute)
        assert route.model == "vision-model"
        assert route.source == "fallback"

    def test_credential_pool_used(self):
        def fake_caps(provider, model, cfg=None):
            if model == "pool-vision":
                return {
                    "image": True,
                    "video": False,
                    "file": True,
                    "extracted-file": True,
                }
            return {
                "image": False,
                "video": False,
                "file": True,
                "extracted-file": True,
            }

        with patch("agent.direct_desktop_runner._capability_for", side_effect=fake_caps):
            route = resolve_multimodal_route(
                ["image"],
                "p",
                "text",
                credential_pool=[{"provider": "p", "model": "pool-vision"}],
            )
        assert isinstance(route, MultimodalRoute)
        assert route.source == "credential_pool"

    def test_video_required_without_video_capability(self):
        with patch(
            "agent.direct_desktop_runner._capability_for",
            return_value={
                "image": True,
                "video": False,
                "file": True,
                "extracted-file": True,
            },
        ):
            result = resolve_multimodal_route(
                ["video"], "anthropic", "claude-sonnet-4"
            )
        assert isinstance(result, MultimodalRouteFailure)


class TestNoStripFlag:
    def test_enable_sets_flag(self):
        agent = SimpleNamespace()
        enable_direct_desktop_no_strip(agent)
        assert is_direct_desktop_no_strip(agent) is True
        assert agent._direct_desktop_media_no_strip is True

    def test_media_provider_unsupported_result_shape(self):
        messages = [{"role": "user", "content": "hi"}]
        result = media_provider_unsupported_result(messages, 3, detail="test")
        assert result["failed"] is True
        assert result["completed"] is False
        assert result["error_code"] == MEDIA_PROVIDER_UNSUPPORTED
        assert result["media_provider_unsupported"] is True
        assert "media_provider_unsupported" in result["final_response"]
        assert result["messages"] is messages


class _FakeAgent:
    """Minimal agent surface for the image-rejection recovery branch."""

    def __init__(self, *, no_strip: bool):
        self._direct_desktop_media_no_strip = no_strip
        self._vision_supported = True
        self.log_prefix = "[test] "
        self.provider = "openrouter"
        self.model = "test"
        self.base_url = ""
        self.api_mode = "chat_completions"
        self._is_anthropic_oauth = False
        self.tools = []
        self.context_compressor = None
        self.ephemeral_system_prompt = None
        self.api_key = "k"
        self._client_kwargs = {}
        self._cached_system_prompt = None
        self._printed = []
        self._persisted = False
        self._status_flushed = False
        self._interrupt_requested = False

    def _vprint(self, msg, force=False):
        self._printed.append(msg)

    def _flush_status_buffer(self):
        self._status_flushed = True

    def _persist_session(self, messages, conversation_history=None):
        self._persisted = True

    def _extract_api_error_context(self, err):
        return {}

    def _invoke_api_request_error_hook(self, **kwargs):
        return None

    def _recover_with_credential_pool(self, **kwargs):
        return False, False

    def _try_shrink_image_parts_in_messages(self, *a, **k):
        return False

    def _try_strip_image_parts_from_tool_messages(self, *a, **k):
        return False

    def _rebuild_anthropic_client(self):
        return None

    def _summarize_api_error(self, err):
        return str(err)

    def _clean_error_message(self, msg):
        return str(msg)

    def _emit_status(self, msg):
        self._printed.append(msg)

    def _buffer_vprint(self, msg):
        self._printed.append(msg)

    def clear_interrupt(self):
        self._interrupt_requested = False

    def _dump_api_request_debug(self, *a, **k):
        return None

    def _try_recover_primary_transport(self, *a, **k):
        return False


class _ImageRejectError(Exception):
    def __init__(self):
        super().__init__("Only 'text' content type is supported.")
        self.status_code = 400
        self.body = "Only 'text' content type is supported."
        self.message = self.body


def _synthetic_image_messages():
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aaa"},
                },
            ],
        }
    ]


class TestNoStripBehaviorSimulated:
    def test_strip_helper_still_works_when_called_directly(self):
        """Legacy strip path remains available for non-desktop sessions."""
        messages = _synthetic_image_messages()
        assert _strip_images_from_messages(messages) is True
        # text part remains
        assert any(
            isinstance(p, dict) and p.get("type") == "text"
            for p in messages[0]["content"]
        )
        assert not any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in messages[0]["content"]
        )

    def test_no_strip_flag_gates_strip_path(self):
        """Simulates conversation_loop branch without needing the full loop.

        When the flag is set the recovery must NOT call
        ``_strip_images_from_messages`` and must build a terminal
        media_provider_unsupported result instead.
        """
        agent = _FakeAgent(no_strip=True)
        messages = _synthetic_image_messages()
        api_messages = _synthetic_image_messages()
        original = [dict(p) for p in messages[0]["content"]]

        # Reproduce the gated branch body purely.
        assert getattr(agent, "_direct_desktop_media_no_strip", False)
        if getattr(agent, "_direct_desktop_media_no_strip", False):
            agent._vision_supported = False
            agent._flush_status_buffer()
            result = media_provider_unsupported_result(
                messages, 1, detail="provider rejected image content"
            )
        else:  # pragma: no cover
            _strip_images_from_messages(messages)
            result = None

        assert result is not None
        assert result["failed"] is True
        assert result["media_provider_unsupported"] is True
        # Images preserved — not stripped.
        assert messages[0]["content"] == original
        assert any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in messages[0]["content"]
        )
        assert agent._status_flushed is True
        assert agent._vision_supported is False
        # api_messages never mutated.
        assert any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in api_messages[0]["content"]
        )

    def test_without_flag_strip_still_allowed(self):
        agent = _FakeAgent(no_strip=False)
        messages = _synthetic_image_messages()
        if getattr(agent, "_direct_desktop_media_no_strip", False):
            raise AssertionError("should not branch to no-strip")
        agent._vision_supported = False
        removed = _strip_images_from_messages(messages)
        assert removed is True
        assert agent._vision_supported is False
        assert not any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in messages[0]["content"]
        )

    def test_conversation_loop_import_has_gate(self):
        """Source-level guard: conversation_loop references the no-strip flag."""
        import inspect
        import agent.conversation_loop as cl

        source = inspect.getsource(cl)
        assert "_direct_desktop_media_no_strip" in source
        assert "media_provider_unsupported" in source
