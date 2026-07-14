"""k-hermes: Camofox is removed; package.json must not eagerly depend on it."""
from pathlib import Path
import json

def test_package_json_has_no_camofox_dependency():
    data = json.loads(Path("package.json").read_text())
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = data.get(key) or {}
        assert "@askjo/camofox-browser" not in deps
