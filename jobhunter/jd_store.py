"""Persist each enriched job's full description to a local text file, for
offline analysis outside the DB (corpus building, embeddings, etc.)."""
from __future__ import annotations

import re
from pathlib import Path

from .config import DATA_DIR

JD_DIR = DATA_DIR / "jd"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(s: str) -> str:
    """external_id is usually a short opaque id, but for manually-imported jobs it's
    the full posting URL (see pipeline.import_manual_job) -- collapse anything that
    isn't filename-safe so "/" and ":" don't get read as path separators."""
    return _UNSAFE_FILENAME_CHARS.sub("_", s)[:200]


def save_jd(*, source: str, external_id: str, title: str, company: str,
            url: str, description: str) -> Path:
    """Write one job's full description to JD_DIR/<source>__<external_id>.txt,
    named after the same (source, external_id) pair used for DB dedup so it's
    stable across DB rebuilds. Overwrites on re-save."""
    JD_DIR.mkdir(parents=True, exist_ok=True)
    path = JD_DIR / f"{source}__{_safe_filename_part(external_id)}.txt"
    header = (
        f"source: {source}\nexternal_id: {external_id}\n"
        f"title: {title}\ncompany: {company}\nurl: {url}\n---\n"
    )
    path.write_text(header + (description or ""), encoding="utf-8")
    return path
