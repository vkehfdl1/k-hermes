"""pytest plugin for Todo 15 Linux runner.

Only seeds user-runtime sockets so systemd preflight can short-circuit in
Docker. Does not override is_container / supports_systemd_services — those
must remain real so hermes unit tests can exercise the branches.
"""
import os
from pathlib import Path


def pytest_configure(config):  # noqa: ARG001
    if os.environ.get("HERMES_T15_LINUX_RUNNER") != "1":
        return
    uid = os.getuid()
    runtime = Path(f"/run/user/{uid}")
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "systemd").mkdir(parents=True, exist_ok=True)
        private = runtime / "systemd" / "private"
        if not private.exists():
            private.touch()
        bus = runtime / "bus"
        if not bus.exists():
            bus.touch()
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime))
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
    except Exception:
        pass
