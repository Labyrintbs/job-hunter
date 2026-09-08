"""Manage jobhunter's scheduled jobs via launchd LaunchAgents.

Not cron: a cron-invoked process runs as a macOS LaunchDaemon in the
non-interactive "Background" security session, which cannot access Keychain
items gated to the user's interactive session. The `claude` CLI stores its
login credential in exactly such a Keychain item, so every cron-invoked LLM
call failed with "Not logged in" even though the CLI works fine when run
interactively -- confirmed directly on this machine (`launchctl managername`
prints "Background" under cron, "Aqua" under a LaunchAgent or a Terminal
session; a LaunchAgent-invoked `claude -p` succeeds where the identical
cron-invoked call fails). Since `jobhunter run`'s daily_run() and watchdog's
triggered catch-up both call judge_one/tailor_one, all three scheduled jobs
need that Aqua-session access, not just `process` -- hence all three are
LaunchAgents here, none are cron entries.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

MARKER = "com.jobhunter.daily"
WATCHDOG_MARKER = "com.jobhunter.watchdog"
PROCESS_MARKER = "com.jobhunter.process"


def _python() -> str:
    """The interpreter to run under launchd. Resolution order: the JOBHUNTER_PYTHON
    env var, the activated conda env's python (CONDA_PREFIX), the project .venv,
    then the interpreter running this code. Preferring conda keeps the env out of
    the iCloud-managed repo tree (macOS storage optimization evicts venv binaries)."""
    override = os.environ.get("JOBHUNTER_PYTHON")
    if override:
        return override
    conda = os.environ.get("CONDA_PREFIX")
    if conda:
        p = Path(conda) / "bin" / "python"
        if p.exists():
            return str(p)
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return sys.executable


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def _command(subcommand: str, log_name: str) -> str:
    py = _python()
    return f"cd {REPO_ROOT} && {py} -m jobhunter.cli {subcommand} >> {REPO_ROOT}/data/{log_name} 2>&1"


def _calendar_intervals(hour: int, minute: int, interval_hours: int | None) -> list[dict]:
    """A fixed daily time, or (if interval_hours is given) a fire time every N
    hours anchored at `hour` -- e.g. hour=2, interval_hours=12 fires at 02:00
    and 14:00."""
    if interval_hours:
        return [{"Hour": (hour + h) % 24, "Minute": minute} for h in range(0, 24, interval_hours)]
    return [{"Hour": hour, "Minute": minute}]


def _describe(label: str, command: str, intervals: list[dict]) -> str:
    times = ", ".join(f"{d['Hour']:02d}:{d['Minute']:02d}" for d in intervals)
    return f"{label} @ {times} -> {command}"


def _write_plist(label: str, command: str, intervals: list[dict]) -> Path:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _plist_path(label)
    with open(path, "wb") as f:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": ["/bin/bash", "-c", command],
            "StartCalendarInterval": intervals,
            "RunAtLoad": False,
        }, f)
    return path


def _load(path: Path) -> None:
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(path)], check=True, capture_output=True)


def _install(label: str, subcommand: str, log_name: str,
            hour: int, minute: int, interval_hours: int | None) -> str:
    command = _command(subcommand, log_name)
    intervals = _calendar_intervals(hour, minute, interval_hours)
    path = _write_plist(label, command, intervals)
    _load(path)
    return _describe(label, command, intervals)


def _uninstall(label: str) -> bool:
    path = _plist_path(label)
    if not path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    return True


def _current(label: str) -> str | None:
    path = _plist_path(label)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        data = plistlib.load(f)
    return _describe(label, " ".join(data["ProgramArguments"][2:]), data["StartCalendarInterval"])


def cron_line(hour: int = 8, minute: int = 0, interval_hours: int | None = None) -> str:
    """Preview string for `jobhunter cron` (no --install): what would be scheduled."""
    return _describe(MARKER, _command("run", "cron.log"),
                     _calendar_intervals(hour, minute, interval_hours))


def install(hour: int = 8, minute: int = 0, interval_hours: int | None = None) -> str:
    return _install(MARKER, "run", "cron.log", hour, minute, interval_hours)


def uninstall() -> bool:
    return _uninstall(MARKER)


def current() -> str | None:
    return _current(MARKER)


def watchdog_cron_line(interval_hours: int = 1) -> str:
    return _describe(WATCHDOG_MARKER, _command("watchdog", "watchdog_cron.log"),
                     _calendar_intervals(0, 0, interval_hours))


def install_watchdog(interval_hours: int = 1) -> str:
    return _install(WATCHDOG_MARKER, "watchdog", "watchdog_cron.log", 0, 0, interval_hours)


def uninstall_watchdog() -> bool:
    return _uninstall(WATCHDOG_MARKER)


def current_watchdog() -> str | None:
    return _current(WATCHDOG_MARKER)


def process_cron_line(interval_hours: int = 1) -> str:
    return _describe(PROCESS_MARKER, _command("process", "process_cron.log"),
                     _calendar_intervals(0, 0, interval_hours))


def install_process(interval_hours: int = 1) -> str:
    return _install(PROCESS_MARKER, "process", "process_cron.log", 0, 0, interval_hours)


def uninstall_process() -> bool:
    return _uninstall(PROCESS_MARKER)


def current_process() -> str | None:
    return _current(PROCESS_MARKER)
