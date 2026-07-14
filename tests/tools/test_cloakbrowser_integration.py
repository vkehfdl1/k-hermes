import logging
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import tools.browser_tool as browser_tool
from tools import cloakbrowser_runtime


WS_URL = "ws://127.0.0.1:9222/fingerprint/hermes_task_1/devtools/browser/abc123"
PEEK_TOKEN = "test-peek-token"


@pytest.fixture(autouse=True)
def _reset_browser_state(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setenv("CLOAKBROWSER_PEEK_TOKEN", PEEK_TOKEN)
    monkeypatch.delenv("CLOAKBROWSER_IDLE_TIMEOUT", raising=False)
    monkeypatch.setenv("CLOAKBROWSER_ROOT", str(tmp_path / "CloakBrowser"))
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_session_last_activity", {})
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(cloakbrowser_runtime, "_process", None, raising=False)
    monkeypatch.setattr(cloakbrowser_runtime, "_generated_peek_token", None, raising=False)
    yield
    proc = getattr(cloakbrowser_runtime, "_process", None)
    if proc is not None and hasattr(proc, "terminate"):
        proc.terminate()
    monkeypatch.setattr(cloakbrowser_runtime, "_process", None, raising=False)


def _install_cloakserve(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "CloakBrowser"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    cloakserve = bin_dir / "cloakserve"
    cloakserve.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    cloakserve.chmod(0o755)
    monkeypatch.setenv("CLOAKBROWSER_ROOT", str(root))
    return root


def _discovery_response(ws_url: str = WS_URL) -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"webSocketDebuggerUrl": ws_url}
    return response


def _peek_status_response(status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    return response


class _RunningProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def test_default_browser_session_uses_cloakbrowser_cdp(monkeypatch, tmp_path):
    # Given: a local CloakBrowser checkout and a configured cloud provider.
    root = _install_cloakserve(tmp_path, monkeypatch)
    provider = Mock()
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")

    requests_get = Mock(side_effect=[
        RuntimeError("not ready"),
        _discovery_response(),
        _peek_status_response(),
    ])
    monkeypatch.setattr(browser_tool.requests, "get", requests_get)
    monkeypatch.setattr(cloakbrowser_runtime.shutil, "which", lambda name: None)
    popen = Mock(return_value=_RunningProcess())
    monkeypatch.setattr(cloakbrowser_runtime.subprocess, "Popen", popen)

    # When: a browser session is created without an explicit CDP override.
    session = browser_tool._get_session_info("task:1")

    # Then: the session is a CloakBrowser CDP session and cloud is not used.
    assert session["cdp_url"] == WS_URL
    assert session["features"]["cloakbrowser"] is True
    assert session["preview_url"] == "http://127.0.0.1:9222/peek?fingerprint=hermes_task_1&token=test-peek-token"
    assert session["cloakbrowser_seed"] == "hermes_task_1"
    provider.create_session.assert_not_called()
    popen.assert_called_once()
    cmd = popen.call_args.args[0]
    assert cmd == [
        str(root / "bin" / "cloakserve"),
        "--host=127.0.0.1",
        "--port=9222",
        "--idle-timeout=600",
        "--headless=false",
    ]
    assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL
    assert popen.call_args.kwargs["cwd"] == str(root)
    assert popen.call_args.kwargs["env"]["CLOAKBROWSER_PEEK_TOKEN"] == PEEK_TOKEN
    assert popen.call_args.kwargs["env"]["CLOAKSERVE_PEEK_TOKEN"] == PEEK_TOKEN


def test_cloakserve_launch_uses_uv_serve_environment(monkeypatch, tmp_path):
    # Given: a local CloakBrowser checkout without a pre-created virtualenv.
    root = _install_cloakserve(tmp_path, monkeypatch)
    provider = Mock()
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(cloakbrowser_runtime.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(browser_tool.requests, "get", Mock(side_effect=[
        RuntimeError("offline"),
        _discovery_response(),
        _peek_status_response(),
    ]))
    popen = Mock(return_value=_RunningProcess())
    monkeypatch.setattr(cloakbrowser_runtime.subprocess, "Popen", popen)

    # When: k-hermes has to start cloakserve itself.
    session = browser_tool._get_session_info("task:uv")

    # Then: it uses the local fork through uv's serve extra instead of relying
    # on whatever python3 happens to be first on PATH.
    assert session["features"]["cloakbrowser"] is True
    assert popen.call_args.args[0] == [
        "/usr/local/bin/uv",
        "run",
        "--extra",
        "serve",
        str(root / "bin" / "cloakserve"),
        "--host=127.0.0.1",
        "--port=9222",
        "--idle-timeout=600",
        "--headless=false",
    ]
    assert popen.call_args.kwargs["env"]["CLOAKBROWSER_PEEK_TOKEN"] == PEEK_TOKEN


def test_cloud_provider_is_skipped_when_cloakbrowser_default_is_enabled(monkeypatch, tmp_path):
    # Given: CloakBrowser is already serving discovery and a provider is configured.
    _install_cloakserve(tmp_path, monkeypatch)
    provider = Mock()
    provider.create_session.side_effect = AssertionError("cloud provider should be skipped")
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(browser_tool.requests, "get", Mock(side_effect=[
        _discovery_response(),
        _peek_status_response(),
    ]))
    popen = Mock(return_value=_RunningProcess())
    monkeypatch.setattr(cloakbrowser_runtime.subprocess, "Popen", popen)

    # When: the default session is created.
    session = browser_tool._get_session_info("public-task")

    # Then: the existing CloakBrowser CDP route is used without cloud or spawn.
    assert session["features"]["cloakbrowser"] is True
    assert session["cdp_url"] == WS_URL
    provider.create_session.assert_not_called()
    popen.assert_not_called()


def test_existing_cloakserve_must_accept_current_peek_token(monkeypatch, tmp_path):
    _install_cloakserve(tmp_path, monkeypatch)
    provider = Mock()
    provider.create_session.side_effect = AssertionError("provider should not be used")
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(browser_tool.requests, "get", Mock(side_effect=[
        _discovery_response(),
        _peek_status_response(403),
    ]))
    popen = Mock(return_value=_RunningProcess())
    monkeypatch.setattr(cloakbrowser_runtime.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="did not accept"):
        browser_tool._get_session_info("public-task")

    provider.create_session.assert_not_called()
    popen.assert_not_called()


def test_explicit_cdp_override_still_wins(monkeypatch, tmp_path):
    # Given: an explicit operator-provided CDP URL.
    _install_cloakserve(tmp_path, monkeypatch)
    override = "ws://operator-host:9222/devtools/browser/operator"
    provider = Mock()
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: override)
    monkeypatch.setattr(
        browser_tool.requests,
        "get",
        Mock(side_effect=AssertionError("cloak discovery should not be called")),
    )

    # When: session info is created.
    session = browser_tool._get_session_info("task-override")

    # Then: the override path is preserved.
    assert session["cdp_url"] == override
    assert session["features"]["cdp_override"] is True
    assert "cloakbrowser" not in session["features"]
    provider.create_session.assert_not_called()


def test_cloakbrowser_seed_sanitizes_task_ids():
    # Given: task IDs with route-unsafe characters and an oversized value.
    task_ids = [
        "task:with/slash and spaces",
        "../../../escape",
        "",
        "__default__",
        "x" * 300,
    ]

    # When: CloakBrowser seeds are derived.
    seeds = [cloakbrowser_runtime.seed_for_task(task_id) for task_id in task_ids]

    # Then: every seed is deterministic, route-safe, and accepted by cloakserve.
    assert seeds[0] == "hermes_task_with_slash_and_spaces"
    assert seeds[1] == "hermes_escape"
    assert seeds[2] == "hermes_default"
    assert seeds[3] != "__default__"
    assert len(seeds[4]) <= 128
    assert all(seed.startswith("hermes_") for seed in seeds)
    assert cloakbrowser_runtime.seed_for_task(task_ids[0]) == seeds[0]
    safe_seed_re = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
    assert all(safe_seed_re.fullmatch(seed) and cloakbrowser_runtime.SAFE_SEED_RE.fullmatch(seed) for seed in seeds)


def test_cloakserve_launch_uses_idle_timeout_and_peek_token_env(monkeypatch, tmp_path):
    root = _install_cloakserve(tmp_path, monkeypatch)
    monkeypatch.setenv("CLOAKBROWSER_IDLE_TIMEOUT", "42")
    monkeypatch.setenv("CLOAKBROWSER_PEEK_TOKEN", "operator-token")
    monkeypatch.setattr(cloakbrowser_runtime.shutil, "which", lambda name: None)
    popen = Mock(return_value=_RunningProcess())
    monkeypatch.setattr(cloakbrowser_runtime.subprocess, "Popen", popen)

    cloakbrowser_runtime.launch_cloakserve()

    assert popen.call_args.args[0] == [
        str(root / "bin" / "cloakserve"),
        "--host=127.0.0.1",
        "--port=9222",
        "--idle-timeout=42",
        "--headless=false",
    ]
    assert popen.call_args.kwargs["env"]["CLOAKBROWSER_PEEK_TOKEN"] == "operator-token"
    assert popen.call_args.kwargs["env"]["CLOAKSERVE_PEEK_TOKEN"] == "operator-token"


def test_preview_url_uses_stable_process_local_token(monkeypatch):
    monkeypatch.delenv("CLOAKBROWSER_PEEK_TOKEN", raising=False)
    monkeypatch.setattr(cloakbrowser_runtime, "_generated_peek_token", None, raising=False)

    first = cloakbrowser_runtime.preview_url("hermes_seed")
    second = cloakbrowser_runtime.preview_url("hermes_seed")

    assert first == second
    assert "token=" in first
    assert "hermes_seed" in first


def test_cleanup_browser_deletes_cloakbrowser_seed(monkeypatch):
    delete_seed = Mock(return_value=True)
    monkeypatch.setattr(cloakbrowser_runtime, "delete_seed", delete_seed)
    monkeypatch.setattr(browser_tool, "_active_sessions", {
        "task-clean": {
            "session_name": "cloak_sess",
            "bb_session_id": None,
            "cloakbrowser_seed": "hermes_task_clean",
            "features": {"cloakbrowser": True},
        }
    })
    monkeypatch.setattr(browser_tool, "_session_last_activity", {"task-clean": 1.0})
    monkeypatch.setattr(browser_tool, "_stop_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(browser_tool, "_maybe_stop_recording", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_run_browser_command", lambda *args, **kwargs: {})

    browser_tool.cleanup_browser("task-clean")

    delete_seed.assert_called_once_with("hermes_task_clean")
    assert "task-clean" not in browser_tool._active_sessions


def test_delete_seed_redacts_tokenized_url_from_exception_logs(monkeypatch, caplog):
    monkeypatch.setenv("CLOAKBROWSER_PEEK_TOKEN", "operator-token")

    def fail_delete(*_args, **_kwargs):
        raise requests.RequestException(
            "failed GET http://127.0.0.1:9222/fingerprint/hermes_task?token=operator-token"
        )

    monkeypatch.setattr(cloakbrowser_runtime.requests, "delete", fail_delete)
    caplog.set_level(logging.DEBUG)

    assert cloakbrowser_runtime.delete_seed("hermes_task") is False
    assert "operator-token" not in caplog.text


def test_missing_cloakbrowser_root_reports_actionable_error(monkeypatch, tmp_path):
    # Given: CLOAKBROWSER_ROOT points to a checkout without bin/cloakserve.
    missing_root = tmp_path / "missing-cloakbrowser"
    monkeypatch.setenv("CLOAKBROWSER_ROOT", str(missing_root))
    provider = Mock()
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
    monkeypatch.setattr(browser_tool.requests, "get", Mock(side_effect=RuntimeError("offline")))
    local_session = Mock(side_effect=AssertionError("agent-browser fallback should not run"))
    monkeypatch.setattr(browser_tool, "_create_local_session", local_session)

    # When / Then: session creation raises a CloakBrowser-specific error.
    with pytest.raises(RuntimeError) as exc_info:
        browser_tool._get_session_info("task-missing")

    message = str(exc_info.value)
    assert str(missing_root / "bin" / "cloakserve") in message
    assert "CLOAKBROWSER_ROOT" in message
    assert "CloakBrowser" in message
    provider.create_session.assert_not_called()
    local_session.assert_not_called()
