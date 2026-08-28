from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR, DB_PATH
from .models import Job

STATUSES = [
    "new",
    "shortlisted",
    "cv_ready",
    "applied",
    "responded",
    "interview",
    "offer",
    "rejected",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT DEFAULT '',
    language      TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    contract_type TEXT DEFAULT '',
    posted_at     TEXT DEFAULT '',
    score         INTEGER DEFAULT 0,
    match_reasons TEXT DEFAULT '',
    fetched_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS applications (
    job_id        INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'new',
    notes         TEXT DEFAULT '',
    submitted_url TEXT DEFAULT '',
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cv_artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    tex_path     TEXT DEFAULT '',
    pdf_path     TEXT DEFAULT '',
    base_version TEXT DEFAULT '',
    generated_at TEXT DEFAULT (datetime('now'))
);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: Path | None = None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _content_key(job: Job) -> str:
    if job.url:
        return job.url
    return f"{job.company.lower()}|{job.title.lower()}|{job.location.lower()}"


def upsert_job(conn: sqlite3.Connection, job: Job, score: int, reasons: str) -> tuple[int, bool]:
    """Insert a job if new. Returns (job_id, is_new). Existing jobs are left untouched
    except for a refreshed score/reasons, preserving their application status.
    Dedups both on (source, external_id) and on content (WTTJ indexes the same posting
    under several objectIDs)."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
        (job.source, job.external_id),
    ).fetchone()
    if not row:
        key = _content_key(job)
        row = conn.execute(
            """SELECT id FROM jobs WHERE source = ?
               AND COALESCE(NULLIF(url, ''), lower(company)||'|'||lower(title)||'|'||lower(location)) = ?""",
            (job.source, key),
        ).fetchone()
    if row:
        conn.execute(
            "UPDATE jobs SET score = ?, match_reasons = ? WHERE id = ?",
            (score, reasons, row["id"]),
        )
        return row["id"], False

    cur = conn.execute(
        """INSERT INTO jobs
           (source, external_id, title, company, location, language, url,
            description, contract_type, posted_at, score, match_reasons)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.source, job.external_id, job.title, job.company, job.location,
            job.language, job.url, job.description, job.contract_type,
            job.posted_at, score, reasons,
        ),
    )
    job_id = cur.lastrowid
    conn.execute("INSERT INTO applications (job_id, status) VALUES (?, 'new')", (job_id,))
    return job_id, True


def list_jobs(conn: sqlite3.Connection, status: str | None = None, min_score: int = 0) -> list[sqlite3.Row]:
    q = """
        SELECT j.*, a.status, a.notes, a.submitted_url,
               (SELECT pdf_path FROM cv_artifacts c WHERE c.job_id = j.id
                ORDER BY c.generated_at DESC LIMIT 1) AS cv_pdf
        FROM jobs j JOIN applications a ON a.job_id = j.id
        WHERE j.score >= ?
    """
    params: list = [min_score]
    if status:
        q += " AND a.status = ?"
        params.append(status)
    q += " ORDER BY j.score DESC, j.fetched_at DESC"
    return conn.execute(q, params).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT j.*, a.status, a.notes, a.submitted_url
           FROM jobs j JOIN applications a ON a.job_id = j.id WHERE j.id = ?""",
        (job_id,),
    ).fetchone()


def update_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    conn.execute(
        "UPDATE applications SET status = ?, updated_at = datetime('now') WHERE job_id = ?",
        (status, job_id),
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
