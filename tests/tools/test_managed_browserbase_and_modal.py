"""Managed browser cloud providers removed from k-hermes.

Modal-managed tests that previously shared this file with Browserbase/Browser Use
live elsewhere or are dropped with the browser providers.
"""

def test_bundled_browserbase_and_browser_use_plugins_removed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "plugins" / "browser"
    assert not (root / "browserbase").exists()
    assert not (root / "browser_use").exists()
    assert not (root / "firecrawl").exists()
