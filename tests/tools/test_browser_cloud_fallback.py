"""Cloud browser fallback tests — bundled providers removed."""
from pathlib import Path


def test_no_bundled_cloud_browser_plugins():
    root = Path(__file__).resolve().parents[2] / "plugins" / "browser"
    assert not (root / "browserbase").exists()
    assert not (root / "firecrawl").exists()
    assert not (root / "browser_use").exists()
