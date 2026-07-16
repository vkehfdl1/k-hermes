from __future__ import annotations

import re
from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "web-automation"
    / "work-management-browser"
    / "SKILL.md"
)


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _description(text: str) -> str:
    match = re.search(r'^description:\s*["\']?(.*?)["\']?$', text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_work_management_skill_metadata_is_loadable_and_concise():
    text = _text()
    description = _description(text)

    assert text.startswith("---\nname: work-management-browser\n")
    assert len(description) <= 60
    assert description.endswith(".")
    assert "requires_toolsets: [browser]" in text
    assert "related_skills: [captcha-solver" in text


def test_work_management_skill_consolidates_supported_sites():
    text = _text()

    for service in ("Asana", "ClickUp", "Confluence", "Jira", "Linear", "Trello"):
        assert f"### {service}" in text or f"### {service} Cloud" in text

    assert "This skill consolidates durable URL, search, and" in text
    assert "Do not use this skill for Airtable, Notion, or GitHub" in text
    assert "Do not use it for Discord messaging" in text


def test_work_management_skill_preserves_browser_and_mutation_invariants():
    text = _text()

    assert "backed by CloakBrowser" in text
    assert "do not use a\npassword-manager skill" in text
    assert "Old ref IDs are stale" in text
    assert "Change only the fields required by" in text
    assert "Do not report completion from a click alone" in text
    assert "No unrelated fields, items, memberships, or permissions changed" in text

    for tool in (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_back",
        "browser_vision",
    ):
        assert f"`{tool}`" in text
