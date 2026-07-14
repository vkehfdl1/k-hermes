"""Tests for mandatory CloakBrowser session creation when cloud providers exist."""
from unittest.mock import Mock

import pytest

import tools.browser_tool as browser_tool


def _reset_session_state(monkeypatch):
    """Clear caches so each test starts fresh."""
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda t: None)


def _cloak_session(task_id: str) -> dict:
    return {
        "session_name": f"cloak-{task_id}",
        "bb_session_id": None,
        "cdp_url": f"ws://127.0.0.1:9222/devtools/browser/{task_id}",
        "features": {"cloakbrowser": True},
    }


class TestCloudProviderSkippedByCloakBrowserDefault:
    """Tests for _get_session_info cloud-provider bypass."""

    def test_cloud_failure_is_skipped_for_cloakbrowser_default(self, monkeypatch):
        """A broken cloud provider is not consulted when CloakBrowser is default."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.side_effect = RuntimeError("401 Unauthorized")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        session = browser_tool._get_session_info("task-1")

        provider.create_session.assert_not_called()
        assert session["features"]["cloakbrowser"] is True
        assert "fallback_from_cloud" not in session
        assert session["cdp_url"] == "ws://127.0.0.1:9222/devtools/browser/task-1"

    def test_cloud_success_is_still_skipped(self, monkeypatch):
        """A healthy cloud provider still does not override CloakBrowser default."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "cloud-sess",
            "bb_session_id": "bb_123",
            "cdp_url": None,
            "features": {"browserbase": True},
        }
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        session = browser_tool._get_session_info("task-2")

        provider.create_session.assert_not_called()
        assert session["session_name"] == "cloak-task-2"
        assert session["features"]["cloakbrowser"] is True
        assert "fallback_from_cloud" not in session
        assert "fallback_reason" not in session

    def test_cloakbrowser_failure_propagates_without_cloud_fallback(self, monkeypatch):
        """CloakBrowser startup failure is terminal instead of falling back to cloud."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.side_effect = RuntimeError("cloud boom")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(
            browser_tool, "_create_cloakbrowser_session",
            Mock(side_effect=RuntimeError("cloak boom")),
        )

        with pytest.raises(RuntimeError, match="cloak boom"):
            browser_tool._get_session_info("task-3")
        provider.create_session.assert_not_called()

    def test_no_provider_uses_cloakbrowser_directly(self, monkeypatch):
        """No provider still creates a CloakBrowser session."""
        _reset_session_state(monkeypatch)

        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        session = browser_tool._get_session_info("task-4")

        assert session["features"]["cloakbrowser"] is True
        assert "fallback_from_cloud" not in session

    def test_cdp_override_bypasses_provider(self, monkeypatch):
        """CDP override takes priority — cloud provider is never consulted."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")
        create_cloakbrowser = Mock(side_effect=AssertionError("CloakBrowser default should not run for explicit CDP"))
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", create_cloakbrowser)

        session = browser_tool._get_session_info("task-5")

        provider.create_session.assert_not_called()
        create_cloakbrowser.assert_not_called()
        assert session["cdp_url"] == "ws://host:9222/devtools/browser/abc"

    def test_skipping_cloud_default_emits_no_fallback_warning(self, monkeypatch, caplog):
        """Skipping cloud for CloakBrowser should not look like a provider failure."""
        _reset_session_state(monkeypatch)

        BrowserbaseProviderFake = type("BrowserbaseProvider", (), {
            "create_session": Mock(side_effect=ConnectionError("timeout")),
        })
        provider = BrowserbaseProviderFake()
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        with caplog.at_level("WARNING", logger="tools.browser_tool"):
            session = browser_tool._get_session_info("task-6")

        assert session["features"]["cloakbrowser"] is True
        assert not any("BrowserbaseProvider" in r.message and "timeout" in r.message for r in caplog.records)

    def test_provider_state_does_not_affect_next_cloakbrowser_task(self, monkeypatch):
        """Provider flakiness does not affect CloakBrowser task sessions."""
        _reset_session_state(monkeypatch)

        call_count = 0

        def create_session_flaky(task_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return {
                "session_name": "cloud-ok",
                "bb_session_id": "bb_999",
                "cdp_url": None,
                "features": {"browserbase": True},
            }

        provider = Mock()
        provider.create_session.side_effect = create_session_flaky
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        s1 = browser_tool._get_session_info("task-a")
        assert s1["session_name"] == "cloak-task-a"

        s2 = browser_tool._get_session_info("task-b")
        assert s2["session_name"] == "cloak-task-b"
        provider.create_session.assert_not_called()

    def test_invalid_cloud_session_is_irrelevant_when_cloakbrowser_default_runs(self, monkeypatch):
        """Invalid cloud session data is ignored because cloud is not used."""
        _reset_session_state(monkeypatch)

        provider = Mock()
        provider.create_session.return_value = None
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)
        monkeypatch.setattr(browser_tool, "_create_cloakbrowser_session", _cloak_session)

        session = browser_tool._get_session_info("task-7")

        provider.create_session.assert_not_called()
        assert session["features"]["cloakbrowser"] is True
        assert "fallback_reason" not in session
