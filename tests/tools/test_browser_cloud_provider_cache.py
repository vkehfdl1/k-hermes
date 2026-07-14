"""_get_cloud_provider caching with no bundled cloud browsers."""
import pytest
import tools.browser_tool as browser_tool


@pytest.fixture(autouse=True)
def _reset_resolver_state(monkeypatch):
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    yield


class TestCloudProviderCachePolicy:
    def test_explicit_local_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "local"}},
        )
        assert browser_tool._get_cloud_provider() is None

    def test_unknown_provider_does_not_resolve(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"cloud_provider": "browserbase"}},
        )
        assert browser_tool._get_cloud_provider() is None

    def test_empty_config_auto_detect_is_none(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {}},
        )
        assert browser_tool._get_cloud_provider() is None
