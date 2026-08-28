"""Export the analytics views to CSV/JSON for Grafana / Metabase.

SQLite (data/jobhunter.db) stays the source of truth; these are convenience dumps
for tools that prefer files or an HTTP CSV source. View names are whitelisted against
db.VIEW_NAMES so the view identifier can never be attacker-controlled SQL.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import db


def _validate(view: str) -> str:
    if view not in db.VIEW_NAMES:
        raise ValueError(f"unknown view '{view}' (known: {', '.join(db.VIEW_NAMES)})")
    return view


def view_rows(conn, view: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {_validate(view)}").fetchall()]


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def export(out_dir: Path, view: str = "all", fmt: str = "csv") -> list[Path]:
    """Write one file per view into out_dir. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    views = db.VIEW_NAMES if view == "all" else [_validate(view)]
    written: list[Path] = []
    with db.connect() as conn:
        for v in views:
            rows = view_rows(conn, v)
            if fmt == "json":
                path = out_dir / f"{v}.json"
                path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            else:
                path = out_dir / f"{v}.csv"
                path.write_text(to_csv(rows), encoding="utf-8")
            written.append(path)
    return written
