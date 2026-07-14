"""Tests for browser-backend routing.

CloakBrowser is now the default browser backend, so URL-based hybrid routing
does not split private URLs into a second ``::local`` sidecar session.  The
legacy sidecar helpers still exist for cleanup/backward-compatible session
metadata, but new navigation stays on the task's CloakBrowser session unless
an explicit operator CDP override owns the session.
"""
from unittest.mock import Mock

import pytest

import tools.browser_tool as browser_tool


@pytest.fixture(autouse=True)
def _reset_routing_state(monkeypatch):
    """Clear module-level caches so each test starts clean."""
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_auto_local_for_private_urls_resolved", False)
    monkeypatch.setattr(browser_tool, "_cached_auto_local_for_private_urls", True)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda t: None)
    # Default: no CDP override, no extra backend
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)


class TestNavigationSessionKey:
    """Tests for _navigation_session_key URL-based routing decisions."""

    def test_public_url_uses_bare_task_id(self, monkeypatch):
        """Public URL with cloud configured still uses the task's CloakBrowser key."""
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "https://github.com/x/y")
        assert key == "default"

    def test_localhost_stays_on_cloakbrowser_session(self, monkeypatch):
        """``localhost`` URL no longer creates a separate local sidecar."""
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "http://localhost:3000/")
        assert key == "default"

    def test_loopback_ipv4_stays_on_cloakbrowser_session(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "http://127.0.0.1:8080/")
        assert key == "default"

    def test_rfc1918_lan_stays_on_cloakbrowser_session(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "http://192.168.1.50:8000/")
        assert key == "default"

    def test_ipv6_loopback_stays_on_cloakbrowser_session(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "http://[::1]:3000/")
        assert key == "default"

    def test_public_ip_literal_uses_bare_task_id(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        key = browser_tool._navigation_session_key("default", "https://8.8.8.8/")
        assert key == "default"

    def test_mdns_local_hostname_stays_on_cloakbrowser_session(self, monkeypatch):
        """``*.local`` / ``*.lan`` / ``*.internal`` hosts do not split sessions."""
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: Mock())
        for host in ("raspberrypi.local", "printer.lan", "db.internal"):
            key = browser_tool._navigation_session_key("default", f"http://{host}/")
            assert key == "default", f"host {host!r} unexpectedly split the CloakBrowser session"

    def test_no_cloud_provider_stays_on_bare_task_id(self, monkeypatch):
        """When cloud provider is not configured, no hybrid routing happens."""
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
        key = browser_tool._navigation_session_key("default", "http://localhost:3000/")
        assert key == "default"

class TestSessionKeyHelpers:
    def test_is_local_sidecar_key(self):
        assert browser_tool._is_local_sidecar_key("default::local")
        assert browser_tool._is_local_sidecar_key("my_task::local")
        assert not browser_tool._is_local_sidecar_key("default")
        assert not browser_tool._is_local_sidecar_key("my_task")

    def test_last_session_key_falls_back_to_task_id(self, monkeypatch):
        """Without a recorded last-active key, returns the bare task_id."""
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {})
        last_session_key = getattr(browser_tool, "_last_session_key")
        assert browser_tool._last_session_key("default") == "default"
        assert browser_tool._last_session_key("task-42") == "task-42"
        assert last_session_key(None) == "default"

    def test_last_session_key_returns_recorded_key(self, monkeypatch):
        monkeypatch.setattr(
            browser_tool,
            "_last_active_session_key",
            {"default": "default::local", "task-42": "task-42"},
        )
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {"default::local": {"session_name": "local_sess"}},
        )
        assert browser_tool._last_session_key("default") == "default::local"
        assert browser_tool._last_session_key("task-42") == "task-42"
        # Unknown task_id still falls back
        assert browser_tool._last_session_key("other") == "other"

    def test_last_session_key_drops_stale_sidecar_binding(self, monkeypatch):
        """A cleaned last-active sidecar must not be silently resurrected."""
        last_active = {"default": "default::local"}
        monkeypatch.setattr(browser_tool, "_last_active_session_key", last_active)
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {"default": {"session_name": "cloud_sess"}},
        )

        assert browser_tool._last_session_key("default") == "default"
        assert last_active == {}

    def test_last_session_key_keeps_bare_task_binding_without_active_session(self, monkeypatch):
        """Bare task fallback preserves historical lazy-create behavior."""
        monkeypatch.setattr(browser_tool, "_last_active_session_key", {"default": "default"})
        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        assert browser_tool._last_session_key("default") == "default"

    def test_last_session_key_drops_mismatched_owner_metadata(self, monkeypatch):
        """Explicit ownership metadata prevents retargeting to another task's session."""
        last_active = {"default": "other-task::local"}
        monkeypatch.setattr(browser_tool, "_last_active_session_key", last_active)
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {
                "other-task::local": {
                    "session_name": "local_sess",
                    "session_key": "other-task::local",
                    "owner_task_id": "other-task",
                }
            },
        )

        assert browser_tool._last_session_key("default") == "default"
        assert last_active == {}


class TestHybridRoutingSessionCreation:
    """_get_session_info uses CloakBrowser even for legacy sidecar-looking keys."""

    def test_local_sidecar_key_skips_cloud_provider(self, monkeypatch):
        """A ``::local``-suffixed legacy key still skips cloud and uses CloakBrowser."""
        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "should_not_be_used",
            "bb_session_id": "bb_xxx",
            "cdp_url": "wss://fake.browserbase.com/ws",
        }
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda t: None)
        monkeypatch.setattr(
            browser_tool,
            "_create_cloakbrowser_session",
            lambda task_id: {
                "session_name": "cloak-sess",
                "bb_session_id": None,
                "cdp_url": "ws://127.0.0.1:9222/devtools/browser/cloak",
                "features": {"cloakbrowser": True},
            },
        )

        session = browser_tool._get_session_info("default::local")

        assert provider.create_session.call_count == 0
        assert session["bb_session_id"] is None
        assert session["cdp_url"] == "ws://127.0.0.1:9222/devtools/browser/cloak"
        assert session["features"]["cloakbrowser"] is True
        assert session["session_key"] == "default::local"
        assert session["owner_task_id"] == "default"

    def test_bare_task_id_with_cloud_provider_uses_cloakbrowser(self, monkeypatch):
        """A bare task_id skips cloud because CloakBrowser is mandatory by default."""
        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "cloud-sess",
            "bb_session_id": "bb_123",
            "cdp_url": "wss://real.browserbase.com/ws",
        }
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
        monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda t: None)
        monkeypatch.setattr(browser_tool, "_resolve_cdp_override", lambda u: u)
        monkeypatch.setattr(
            browser_tool,
            "_create_cloakbrowser_session",
            lambda task_id: {
                "session_name": "cloak-sess",
                "bb_session_id": None,
                "cdp_url": "ws://127.0.0.1:9222/devtools/browser/cloak",
                "features": {"cloakbrowser": True},
            },
        )

        session = browser_tool._get_session_info("default")

        assert provider.create_session.call_count == 0
        assert session["bb_session_id"] is None
        assert session["features"]["cloakbrowser"] is True
        assert session["session_key"] == "default"
        assert session["owner_task_id"] == "default"


class TestCleanupHybridSessions:
    """cleanup_browser(bare_task_id) must reap both cloud + local sidecar sessions."""

    def test_cleanup_reaps_both_primary_and_sidecar(self, monkeypatch):
        """Given a bare task_id with both sessions alive, both get cleaned."""
        reaped = []

        def _fake_cleanup_one(key):
            reaped.append(key)

        monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", _fake_cleanup_one)
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {
                "default": {"session_name": "cloud_sess"},
                "default::local": {"session_name": "local_sess"},
            },
        )
        monkeypatch.setattr(
            browser_tool, "_last_active_session_key", {"default": "default::local"}
        )

        browser_tool.cleanup_browser("default")

        assert set(reaped) == {"default", "default::local"}
        # last-active pointer dropped
        assert "default" not in browser_tool._last_active_session_key

    def test_cleanup_reaps_only_primary_when_no_sidecar(self, monkeypatch):
        """When no sidecar exists, only the primary is reaped."""
        reaped = []

        def _fake_cleanup_one(key):
            reaped.append(key)

        monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", _fake_cleanup_one)
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {"default": {"session_name": "cloud_sess"}},
        )

        browser_tool.cleanup_browser("default")

        assert reaped == ["default"]

    def test_cleanup_sidecar_directly_keeps_primary(self, monkeypatch):
        """Calling cleanup with a ``::local`` key reaps only the sidecar."""
        reaped = []

        def _fake_cleanup_one(key):
            reaped.append(key)

        monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", _fake_cleanup_one)
        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {
                "default": {"session_name": "cloud_sess"},
                "default::local": {"session_name": "local_sess"},
            },
        )
        monkeypatch.setattr(
            browser_tool, "_last_active_session_key", {"default": "default::local"}
        )

        browser_tool.cleanup_browser("default::local")

        assert reaped == ["default::local"]
        # The cleaned sidecar must not remain the recorded owner; otherwise a
        # later click/snapshot could resurrect it instead of using the primary.
        assert "default" not in browser_tool._last_active_session_key
