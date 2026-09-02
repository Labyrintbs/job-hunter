"""Manage a daily crontab entry that runs the pipeline.

The entry is tagged with a marker comment so we can find/replace/remove exactly
our line and never touch the user's other cron jobs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT

MARKER = "# jobhunter-daily"
WATCHDOG_MARKER = "# jobhunter-watchdog"


def _python() -> str:
    """The interpreter to run under cron. Resolution order: the JOBHUNTER_PYTHON
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


def cron_line(hour: int = 8, minute: int = 0) -> str:
    py = _python()
    cmd = f"cd {REPO_ROOT} && {py} -m jobhunter.cli run >> {REPO_ROOT}/data/cron.log 2>&1"
    return f"{minute} {hour} * * * {cmd} {MARKER}"


def _read_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def without_marker(existing: str, marker: str = MARKER) -> str:
    return "\n".join(l for l in existing.splitlines() if marker not in l)


def with_entry(existing: str, line: str, marker: str = MARKER) -> str:
    base = without_marker(existing, marker).rstrip("\n")
    return (base + "\n" if base else "") + line + "\n"


def install(hour: int = 8, minute: int = 0) -> str:
    line = cron_line(hour, minute)
    _write_crontab(with_entry(_read_crontab(), line, MARKER))
    return line


def uninstall() -> bool:
    existing = _read_crontab()
    if MARKER not in existing:
        return False
    remaining = without_marker(existing, MARKER).strip()
    _write_crontab(remaining + "\n" if remaining else "")
    return True


def current() -> str | None:
    for l in _read_crontab().splitlines():
        if MARKER in l:
            return l
    return None


def watchdog_cron_line(interval_hours: int = 1) -> str:
    """Runs `jobhunter watchdog` every interval_hours; it self-checks staleness
    and only refetches if the gap exceeds its own --max-gap-hours threshold."""
    py = _python()
    cmd = f"cd {REPO_ROOT} && {py} -m jobhunter.cli watchdog >> {REPO_ROOT}/data/watchdog_cron.log 2>&1"
    return f"0 */{interval_hours} * * * {cmd} {WATCHDOG_MARKER}"


def install_watchdog(interval_hours: int = 1) -> str:
    line = watchdog_cron_line(interval_hours)
    _write_crontab(with_entry(_read_crontab(), line, WATCHDOG_MARKER))
    return line


def uninstall_watchdog() -> bool:
    existing = _read_crontab()
    if WATCHDOG_MARKER not in existing:
        return False
    remaining = without_marker(existing, WATCHDOG_MARKER).strip()
    _write_crontab(remaining + "\n" if remaining else "")
    return True


def current_watchdog() -> str | None:
    for l in _read_crontab().splitlines():
        if WATCHDOG_MARKER in l:
            return l
    return None
