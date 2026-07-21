from __future__ import annotations

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
DEFAULT_STARTUP_TIMEOUT = 30
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


def _peek_token_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cloakbrowser-peek-token"


def peek_token() -> str:
    """Return the peek token shared with cloakserve.

    Precedence: explicit env var, then the token persisted under
    ``HERMES_HOME``, then a freshly generated one (persisted for reuse).
    Persistence matters because cloakserve outlives the launching process
    (one-shot desktop turns, restarted CLIs): a per-process random token
    would make every follow-up process fail ``/peek/status`` against the
    surviving cloakserve and hard-error the browser tools.
    """
    global _generated_peek_token
    configured = os.environ.get("CLOAKBROWSER_PEEK_TOKEN", "").strip()
    if configured:
        return configured
    if _generated_peek_token is None:
        path = _peek_token_path()
        try:
            persisted = path.read_text(encoding="utf-8").strip()
        except OSError:
            persisted = ""
        if persisted:
            _generated_peek_token = persisted
        else:
            _generated_peek_token = secrets.token_urlsafe(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600, exist_ok=True)
                path.write_text(_generated_peek_token, encoding="utf-8")
            except OSError:
                logger.debug("Could not persist CloakBrowser peek token", exc_info=True)
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
    """Locate the CloakBrowser checkout containing ``bin/cloakserve``.

    An explicit ``CLOAKBROWSER_ROOT`` always wins (even when broken, so the
    operator gets an actionable error about the path they configured).
    Otherwise scan the known layouts and return the first checkout that
    actually has ``bin/cloakserve``:

    1. Sibling of this k-hermes checkout (symlinks resolved) — dev layout.
    2. Sibling of the *unresolved* file location — a symlinked managed
       checkout (e.g. ``~/.dolshoi/k-hermes`` -> a dev tree) whose real
       parent differs from its logical parent.
    3. ``~/.hermes/CloakBrowser`` — the managed data-dir clone.
    """
    configured = os.environ.get("CLOAKBROWSER_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()

    candidates = _candidate_roots()
    for candidate in candidates:
        if (candidate / "bin" / "cloakserve").is_file():
            return candidate
    # Nothing found: return the primary candidate so _cloakserve_executable
    # raises its actionable "set CLOAKBROWSER_ROOT" error for that path.
    return candidates[0]


def _candidate_roots() -> list[Path]:
    return [
        Path(__file__).resolve().parents[2] / "CloakBrowser",
        Path(__file__).absolute().parents[2] / "CloakBrowser",
        Path.home() / ".hermes" / "CloakBrowser",
    ]


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


def _log_path() -> Path:
    from hermes_constants import get_hermes_home

    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "cloakserve.log"


def _log_tail(max_bytes: int = 4096) -> str:
    """Return the tail of the cloakserve log for actionable error messages."""
    try:
        path = _log_path()
        data = path.read_bytes()
        return data[-max_bytes:].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def launch_cloakserve() -> None:
    global _process
    root = _find_root()
    cloakserve = _cloakserve_executable(root)
    cmd = launch_command(root, cloakserve)
    # PYTHONPATH: put the CloakBrowser checkout first so `import cloakbrowser`
    # resolves from the checkout itself even when the checkout's .venv has a
    # stale/broken editable install (a real observed failure mode: the venv's
    # .pth pointed at a moved directory and cloakserve died on import).
    launch_env = dict(os.environ)
    existing_pythonpath = launch_env.get("PYTHONPATH", "")
    launch_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(root), existing_pythonpath) if part
    )
    launch_env["CLOAKBROWSER_PEEK_TOKEN"] = peek_token()
    launch_env["CLOAKSERVE_PEEK_TOKEN"] = peek_token()
    # Capture output to a log file instead of DEVNULL so a crashed cloakserve
    # leaves an actionable trace (surfaced in ensure_cdp_url's error message).
    try:
        log_file = open(_log_path(), "ab")
    except OSError:
        log_file = subprocess.DEVNULL
    popen_extra = {
        "cwd": str(root),
        "stdout": log_file,
        "stderr": log_file,
        "stdin": subprocess.DEVNULL,
        "env": launch_env,
    }
    if os.name == "nt":
        popen_extra["creationflags"] = windows_hide_flags()
        popen_extra["close_fds"] = True
    else:
        popen_extra["start_new_session"] = True
    try:
        # stdin is in popen_extra; pass it explicitly so the TUI stdin-guard
        # static check sees the keyword on the call site.
        _process = subprocess.Popen(
            cmd,
            stdin=popen_extra.get("stdin", subprocess.DEVNULL),
            **{k: v for k, v in popen_extra.items() if k != "stdin"},
        )
    except OSError as exc:
        raise RuntimeError(
            "Failed to start CloakBrowser cloakserve from "
            f"{cloakserve}: {exc}"
        ) from exc
    finally:
        if log_file is not subprocess.DEVNULL:
            log_file.close()


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


def _terminate_stale_cloakserve() -> bool:
    """Terminate a cloakserve on our port that rejects the current peek token.

    The loopback port is product-owned; a listener there that fails
    ``/peek/status`` is a stale cloakserve from a previous session whose
    token no longer matches. Replacing it automatically keeps browser tools
    working instead of hard-erroring with a "restart it yourself" message.
    Only processes whose command line mentions cloakserve are touched.
    """
    try:
        import psutil
    except ImportError:
        return False

    target_port = port()
    killed = False
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "cloakserve" not in cmdline:
                continue
            listens_here = any(
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == target_port
                for conn in proc.net_connections(kind="inet")
            )
            if not listens_here:
                continue
            logger.warning(
                "Replacing stale cloakserve (pid %s) that rejected the current peek token",
                proc.pid,
            )
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    # A zombie (dead but unreaped by its parent) no longer
                    # holds the port; treat it as replaced.
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        raise
            killed = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return killed


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
        # A live cloakserve that rejects our token is stale (previous session,
        # different token). Self-heal: replace it and fall through to launch.
        if not _terminate_stale_cloakserve():
            raise RuntimeError(
                "CloakBrowser CDP is reachable but /peek/status did not accept "
                "the configured CLOAKBROWSER_PEEK_TOKEN, and the stale "
                "cloakserve process could not be replaced automatically. Stop "
                "the existing cloakserve process or restart it with the same "
                "token."
            )

    with _lock:
        if not process_running():
            launch_cloakserve()

    timeout_s = max(1, _env_int("CLOAKBROWSER_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT))
    deadline = time.monotonic() + timeout_s
    last_cdp_url = cdp_url
    while time.monotonic() < deadline:
        proc = _process
        if proc is not None and proc.poll() is not None:
            # Fail fast with the crash output instead of burning the full
            # timeout on a process that already died (e.g. import errors).
            tail = _log_tail()
            detail = f"\n--- cloakserve log tail ---\n{tail}" if tail else ""
            raise RuntimeError(
                "CloakBrowser cloakserve exited immediately with code "
                f"{proc.returncode}.{detail}"
            )
        last_cdp_url = resolve_cdp_url(url)
        if _resolved_cdp_url(last_cdp_url) and _peek_token_accepted(seed):
            return last_cdp_url
        time.sleep(0.1)

    tail = _log_tail()
    detail = f"\n--- cloakserve log tail ---\n{tail}" if tail else ""
    raise RuntimeError(
        "CloakBrowser cloakserve did not expose a CDP websocket at "
        f"{url} within {timeout_s}s; last response resolved to "
        f"{redact_url(last_cdp_url)}.{detail}"
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


# NOTE: stop_cloakserve is intentionally NOT registered with atexit. The
# desktop shell runs one-shot event_runner processes per turn; killing
# cloakserve when the launcher exits would tear down the live browser (and
# the peek surface) at the end of every turn. cloakserve reaps itself via
# --idle-timeout, and the host persists the peek token precisely so a
# surviving cloakserve can serve the next session.
