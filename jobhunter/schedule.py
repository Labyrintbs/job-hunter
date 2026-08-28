"""Manage a daily crontab entry that runs the pipeline.

The entry is tagged with a marker comment so we can find/replace/remove exactly
our line and never touch the user's other cron jobs.
"""
from __future__ import annotations

import subprocess
import sys

from .config import REPO_ROOT

MARKER = "# jobhunter-daily"


def _python() -> str:
    """The interpreter to run under cron — prefer the project venv."""
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def cron_line(hour: int = 8, minute: int = 0) -> str:
    py = _python()
    cmd = f"cd {REPO_ROOT} && {py} -m jobhunter.cli run >> {REPO_ROOT}/data/cron.log 2>&1"
    return f"{minute} {hour} * * * {cmd} {MARKER}"


def _read_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _write_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def without_marker(existing: str) -> str:
    return "\n".join(l for l in existing.splitlines() if MARKER not in l)


def with_entry(existing: str, line: str) -> str:
    base = without_marker(existing).rstrip("\n")
    return (base + "\n" if base else "") + line + "\n"


def install(hour: int = 8, minute: int = 0) -> str:
    line = cron_line(hour, minute)
    _write_crontab(with_entry(_read_crontab(), line))
    return line


def uninstall() -> bool:
    existing = _read_crontab()
    if MARKER not in existing:
        return False
    remaining = without_marker(existing).strip()
    _write_crontab(remaining + "\n" if remaining else "")
    return True


def current() -> str | None:
    for l in _read_crontab().splitlines():
        if MARKER in l:
            return l
    return None
