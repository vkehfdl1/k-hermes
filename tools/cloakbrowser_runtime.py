from __future__ import annotations

import atexit
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import requests

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9222
DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_STARTUP_TIMEOUT = 10
SAFE_SEED_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_process: subprocess.Popen | None = None
_lock = threading.Lock()
_generated_peek_token: str | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.debug("Invalid %s value; using default", name)
        return default


def peek_token() -> str:
    global _generated_peek_token
    configured = os.environ.get("CLOAKBROWSER_PEEK_TOKEN", "").strip()
    if configured:
        return configured
    if _generated_peek_token is None:
        _generated_peek_token = secrets.token_urlsafe(32)
    return _generated_peek_token


def port() -> int:
    return _env_int("CLOAKBROWSER_PORT", DEFAULT_PORT)


def base_url() -> str:
    return f"http://127.0.0.1:{port()}"


def seed_for_task(task_id: str | None) -> str:
    raw = str(task_id or "default").strip() or "default"
    seed_body = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "default"
    seed = f"hermes_{seed_body}"[:128]
    if not SAFE_SEED_RE.fullmatch(seed):
        return "hermes_default"
    return seed


def discovery_url(seed: str) -> str:
    return f"{base_url()}/json/version?fingerprint={quote(seed, safe='')}"


def preview_url(seed: str) -> str:
    token = peek_token()
    return (
        f"{base_url()}/peek?fingerprint={quote(seed, safe='')}"
        f"&token={quote(token, safe='')}"
    )


def peek_status_url(seed: str) -> str:
    return (
        f"{base_url()}/peek/status?fingerprint={quote(seed, safe='')}"
        f"&token={quote(peek_token(), safe='')}"
    )


def _find_root() -> Path:
    configured = os.environ.get("CLOAKBROWSER_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "CloakBrowser"


def _cloakserve_executable(root: Path) -> Path:
    cloakserve = root / "bin" / "cloakserve"
    if not cloakserve.is_file():
        raise RuntimeError(
            "CloakBrowser cloakserve not found at "
            f"{cloakserve}. Set CLOAKBROWSER_ROOT to the local CloakBrowser "
            "checkout containing bin/cloakserve."
        )
    if os.name != "nt" and not os.access(cloakserve, os.X_OK):
        raise RuntimeError(
            "CloakBrowser cloakserve is not executable at "
            f"{cloakserve}. Run chmod +x on bin/cloakserve or fix CLOAKBROWSER_ROOT."
        )
    return cloakserve


def process_running() -> bool:
    return _process is not None and _process.poll() is None


def _base_command(root: Path, cloakserve: Path) -> list[str]:
    configured_python = os.environ.get("CLOAKBROWSER_PYTHON", "").strip()
    if configured_python:
        return [configured_python, str(cloakserve)]

    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_python.is_file():
        return [str(venv_python), str(cloakserve)]

    uv_path = shutil.which("uv")
    if uv_path:
        return [uv_path, "run", "--extra", "serve", str(cloakserve)]

    return [str(cloakserve)]


def _headless_enabled() -> bool:
    # Default to a visible (headed) browser so the operator can watch the
    # live session and the desktop peek panel has a real window to mirror.
    # Opt back into headless with CLOAKBROWSER_HEADLESS=1/true/yes/on.
    raw = os.environ.get("CLOAKBROWSER_HEADLESS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def launch_command(root: Path, cloakserve: Path) -> list[str]:
    idle_timeout = max(0, _env_int("CLOAKBROWSER_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT))
    cmd = [
        *_base_command(root, cloakserve),
        "--host=127.0.0.1",
        f"--port={port()}",
        f"--idle-timeout={idle_timeout}",
    ]
    if not _headless_enabled():
        cmd.append("--headless=false")
    return cmd


def launch_cloakserve() -> None:
    global _process
    root = _find_root()
    cloakserve = _cloakserve_executable(root)
    cmd = launch_command(root, cloakserve)
    popen_extra = {
        "cwd": str(root),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "env": {
            **os.environ,
            "CLOAKBROWSER_PEEK_TOKEN": peek_token(),
            "CLOAKSERVE_PEEK_TOKEN": peek_token(),
        },
    }
    if os.name == "nt":
        popen_extra["creationflags"] = windows_hide_flags()
        popen_extra["close_fds"] = True
    else:
        popen_extra["start_new_session"] = True
    try:
        _process = subprocess.Popen(cmd, **popen_extra)
    except OSError as exc:
        raise RuntimeError(
            "Failed to start CloakBrowser cloakserve from "
            f"{cloakserve}: {exc}"
        ) from exc


def _resolved_cdp_url(cdp_url: str) -> bool:
    lowered = cdp_url.lower()
    return lowered.startswith(("ws://", "wss://")) and "/devtools/browser/" in lowered


def _peek_token_accepted(seed: str) -> bool:
    try:
        response = requests.get(peek_status_url(seed), timeout=2)
    except requests.RequestException as exc:
        logger.debug("CloakBrowser peek token check failed for %s: %s", seed, type(exc).__name__)
        return False
    return response.status_code == 200


def ensure_cdp_url(
    task_id: str | None,
    resolve_cdp_url: Callable[[str], str],
    redact_url: Callable[[object], str],
) -> str:
    seed = seed_for_task(task_id)
    url = discovery_url(seed)
    cdp_url = resolve_cdp_url(url)
    if _resolved_cdp_url(cdp_url):
        if _peek_token_accepted(seed):
            return cdp_url
        raise RuntimeError(
            "CloakBrowser CDP is reachable but /peek/status did not accept "
            "the configured CLOAKBROWSER_PEEK_TOKEN. Stop the existing "
            "cloakserve process or restart it with the same token."
        )

    with _lock:
        if not process_running():
            launch_cloakserve()

    timeout_s = max(1, _env_int("CLOAKBROWSER_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT))
    deadline = time.monotonic() + timeout_s
    last_cdp_url = cdp_url
    while time.monotonic() < deadline:
        last_cdp_url = resolve_cdp_url(url)
        if _resolved_cdp_url(last_cdp_url) and _peek_token_accepted(seed):
            return last_cdp_url
        time.sleep(0.1)

    raise RuntimeError(
        "CloakBrowser cloakserve did not expose a CDP websocket at "
        f"{url} within {timeout_s}s; last response resolved to "
        f"{redact_url(last_cdp_url)}."
    )


def create_session(
    task_id: str,
    resolve_cdp_url: Callable[[str], str],
    redact_url: Callable[[object], str],
) -> dict[str, object]:
    seed = seed_for_task(task_id)
    return {
        "session_name": f"cloak_{uuid.uuid4().hex[:10]}",
        "bb_session_id": None,
        "cdp_url": ensure_cdp_url(task_id, resolve_cdp_url, redact_url),
        "preview_url": preview_url(seed),
        "cloakbrowser_seed": seed,
        "features": {"cloakbrowser": True},
    }


def delete_seed(seed: str) -> bool:
    if not SAFE_SEED_RE.fullmatch(seed):
        logger.warning("Skipping CloakBrowser cleanup for invalid seed %r", seed)
        return False

    url = f"{base_url()}/fingerprint/{quote(seed, safe='')}?token={quote(peek_token(), safe='')}"
    try:
        response = requests.delete(url, timeout=5)
    except requests.RequestException as exc:
        logger.debug("CloakBrowser seed cleanup failed for %s: %s", seed, type(exc).__name__)
        return False
    if response.status_code in {200, 202, 204, 404}:
        logger.debug("CloakBrowser seed cleanup status for %s: %s", seed, response.status_code)
        return response.status_code != 404
    logger.warning("CloakBrowser seed cleanup for %s returned HTTP %s", seed, response.status_code)
    return False


def stop_cloakserve() -> None:
    global _process
    with _lock:
        proc = _process
        _process = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


atexit.register(stop_cloakserve)
