from __future__ import annotations

import re
from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "web-automation"
    / "captcha-solver"
    / "SKILL.md"
)


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _description(text: str) -> str:
    match = re.search(r'^description:\s*["\']?(.*?)["\']?$', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_captcha_skill_metadata_is_loadable_and_concise():
    text = _text()
    description = _description(text)

    assert text.startswith("---\nname: captcha-solver\n")
    assert len(description) <= 60
    assert description.endswith(".")
    assert "requires_toolsets: [browser]" in text
    assert "platforms: [linux, macos, windows]" in text


def test_captcha_skill_uses_cloakbrowser_native_workflow():
    text = _text()

    assert "MUST be the normal k-hermes browser session" in text
    assert "backed by CloakBrowser" in text
    for tool in (
        "browser_navigate",
        "browser_snapshot",
        "browser_vision",
        "browser_click",
        "browser_type",
        "browser_press",
    ):
        assert f"`{tool}`" in text


def test_captcha_skill_rejects_unavailable_or_unsafe_shortcuts():
    text = _text()

    assert "No REPL globals" in text
    assert "Never send the screenshot to an external OCR or CAPTCHA-solving service" in text
    assert "Do not substitute coordinate injection" in text
    assert "one failed visual attempt is enough" in text
    assert "ask the user to complete it" in text
