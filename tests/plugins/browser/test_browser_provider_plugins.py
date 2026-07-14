"""k-hermes: bundled cloud browser plugins removed.

Third-party plugins may still register via agent.browser_registry; the
bundled browserbase/firecrawl/browser-use providers are gone.
"""
from __future__ import annotations

import pytest


def _ensure_plugins_loaded() -> None:
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered()


def test_no_bundled_browser_providers_registered() -> None:
    _ensure_plugins_loaded()
    from agent.browser_registry import list_providers

    names = {p.name for p in list_providers()}
    assert "browserbase" not in names
    assert "firecrawl" not in names
    assert "browser-use" not in names


def test_legacy_walk_is_empty_without_third_party_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_plugins_loaded()
    from agent.browser_registry import _resolve, _reset_for_tests, list_providers

    # Even with browserbase-looking env vars, nothing is auto-selected.
    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    assert _resolve(None) is None
