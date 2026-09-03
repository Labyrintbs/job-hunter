"""Self-healing check for the daily cron: if the last fetch is older than
max_gap_hours, run one now and log the decision.

Backstop for cron entries that get silently skipped -- e.g. plain macOS cron
does not catch up missed triggers if the machine was asleep at 8am.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db
from .config import DATA_DIR
from .pipeline import daily_run

LOG_PATH = DATA_DIR / "watchdog.log"


def _log(line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts} UTC] {line}\n")


def check_and_fetch(max_gap_hours: float = 15.0) -> dict:
    """Compare now to the last fetch_runs.ran_at (stored in UTC). If the gap is
    >= max_gap_hours (or there's no prior run at all), trigger a catch-up run."""
    db.init_db()
    with db.connect() as conn:
        last = db.last_fetch_at(conn)

    gap_hours: float | None = None
    if last is not None:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        gap_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600

    stale = last is None or gap_hours >= max_gap_hours

    if not stale:
        _log(f"ok — last run {gap_hours:.1f}h ago (< {max_gap_hours}h threshold), skipping")
        return {"triggered": False, "gap_hours": gap_hours, "last_run": last}

    gap_desc = "no prior runs found" if last is None else f"{gap_hours:.1f}h since last run"
    _log(f"stale — {gap_desc} (>= {max_gap_hours}h threshold), triggering catch-up fetch")
    try:
        summary = daily_run()
    except Exception as exc:
        _log(f"catch-up fetch FAILED — {exc!r}")
        raise
    _log(f"catch-up fetch done — fetched={summary['fetched']} kept={summary['kept']} "
         f"new={summary['new']} judged={summary.get('judged', 0)}")
    return {"triggered": True, "gap_hours": gap_hours, "last_run": last, "summary": summary}
