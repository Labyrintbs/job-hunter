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

CREATE TABLE IF NOT EXISTS filter_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- negative_kw | company_block
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'learned',   -- learned | manual
    weight     INTEGER DEFAULT 15,
    active      INTEGER DEFAULT 0,      -- learned rules start inactive; you approve them
    evidence   TEXT DEFAULT '',
    hit_count  INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS preference_profile (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    n_pos      INTEGER DEFAULT 0,
    n_neg      INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT DEFAULT (datetime('now')),
    fetched     INTEGER DEFAULT 0,
    kept        INTEGER DEFAULT 0,
    new         INTEGER DEFAULT 0,
    filtered_new INTEGER DEFAULT 0,
    by_source   TEXT DEFAULT '',        -- JSON {source: new_count}
    new_idf     INTEGER DEFAULT 0,
    new_france  INTEGER DEFAULT 0,
    new_remote  INTEGER DEFAULT 0,
    new_outside INTEGER DEFAULT 0
);

-- Grafana/Metabase read these directly. Views are recreated each init to stay current.
DROP VIEW IF EXISTS v_new_jobs_by_day;
CREATE VIEW v_new_jobs_by_day AS
    SELECT date(fetched_at) AS day,
           COALESCE(NULLIF(geo_tier,''),'unknown') AS geo_tier,
           COUNT(*) AS n
    FROM jobs GROUP BY day, geo_tier;

DROP VIEW IF EXISTS v_market_by_run;
CREATE VIEW v_market_by_run AS
    SELECT date(ran_at) AS day, COUNT(*) AS runs,
           SUM(new) AS new, SUM(filtered_new) AS filtered_new,
           SUM(new_idf) AS new_idf, SUM(new_france) AS new_france,
           SUM(new_remote) AS new_remote, SUM(new_outside) AS new_outside
    FROM fetch_runs GROUP BY day;

DROP VIEW IF EXISTS v_top_companies;
CREATE VIEW v_top_companies AS
    SELECT company, COUNT(*) AS n,
           SUM(CASE WHEN COALESCE(filtered,0)=0 THEN 1 ELSE 0 END) AS n_active
    FROM jobs GROUP BY company ORDER BY n DESC;

DROP VIEW IF EXISTS v_score_seniority_mix;
CREATE VIEW v_score_seniority_mix AS
    SELECT COALESCE(NULLIF(seniority,''),'unknown') AS seniority,
           COUNT(*) AS n, ROUND(AVG(score),1) AS avg_score,
           SUM(CASE WHEN score>=60 THEN 1 ELSE 0 END) AS n_score_60plus,
           SUM(CASE WHEN score>=40 AND score<60 THEN 1 ELSE 0 END) AS n_score_40_59,
           SUM(CASE WHEN score<40 THEN 1 ELSE 0 END) AS n_score_lt40
    FROM jobs GROUP BY seniority;
"""

VIEW_NAMES = ["v_new_jobs_by_day", "v_market_by_run", "v_top_companies", "v_score_seniority_mix"]


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


MIGRATIONS = {
    "jobs": {
        "llm_score": "INTEGER DEFAULT NULL",
        "llm_verdict": "TEXT DEFAULT ''",
        "llm_reasons": "TEXT DEFAULT ''",
        "seniority": "TEXT DEFAULT ''",
        "min_years": "INTEGER DEFAULT NULL",
        "filtered": "INTEGER DEFAULT 0",
        "filter_reason": "TEXT DEFAULT ''",
        "user_label": "TEXT DEFAULT ''",
        "dismiss_reasons": "TEXT DEFAULT ''",
        "labeled_at": "TEXT DEFAULT ''",
        "description_full": "INTEGER DEFAULT 0",
        "was_filtered": "INTEGER DEFAULT 0",
        "geo_tier": "TEXT DEFAULT ''",
        "last_seen": "TEXT DEFAULT ''",
    },
    "applications": {
        "cover_letter_path": "TEXT DEFAULT ''",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _backfill_geo_tier(conn: sqlite3.Connection) -> None:
    """Populate geo_tier for pre-existing rows (new rows get it at upsert time)."""
    rows = conn.execute("SELECT id, location FROM jobs WHERE COALESCE(geo_tier,'') = ''").fetchall()
    if not rows:
        return
    from . import match
    from .config import load_search_config
    try:
        config = load_search_config()
    except Exception:
        config = {"locations": ["paris", "ile-de-france", "île-de-france"], "allow_remote_france": True}
    for r in rows:
        conn.execute("UPDATE jobs SET geo_tier = ? WHERE id = ?",
                     (match.geo_tier(r["location"], config), r["id"]))


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _backfill_geo_tier(conn)


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


def upsert_job(conn: sqlite3.Connection, job: Job, score: int, reasons: str, *,
               filtered: bool = False, filter_reason: str = "",
               seniority: str = "", min_years: int | None = None,
               geo_tier: str = "") -> tuple[int, bool]:
    """Insert a job if new. Returns (job_id, is_new). Existing jobs keep their
    application status; their score/reasons and screening flags are refreshed, and
    last_seen is stamped every time the job is seen (for staleness / market signals).
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
            """UPDATE jobs SET score = ?, match_reasons = ?, filtered = ?,
               filter_reason = ?, seniority = ?, min_years = ?, geo_tier = ?,
               last_seen = datetime('now'),
               was_filtered = CASE WHEN ? THEN 1 ELSE was_filtered END WHERE id = ?""",
            (score, reasons, int(filtered), filter_reason, seniority, min_years, geo_tier,
             int(filtered), row["id"]),
        )
        return row["id"], False

    cur = conn.execute(
        """INSERT INTO jobs
           (source, external_id, title, company, location, language, url,
            description, contract_type, posted_at, score, match_reasons,
            filtered, filter_reason, seniority, min_years, description_full, was_filtered,
            geo_tier, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            job.source, job.external_id, job.title, job.company, job.location,
            job.language, job.url, job.description, job.contract_type,
            job.posted_at, score, reasons,
            int(filtered), filter_reason, seniority, min_years,
            int(len(job.description or "") > 200), int(filtered), geo_tier,
        ),
    )
    job_id = cur.lastrowid
    conn.execute("INSERT INTO applications (job_id, status) VALUES (?, 'new')", (job_id,))
    return job_id, True


def add_fetch_run(conn: sqlite3.Connection, stats: dict) -> int:
    """Persist one fetch run's aggregate counts (the market time-series)."""
    import json
    cur = conn.execute(
        """INSERT INTO fetch_runs
           (fetched, kept, new, filtered_new, by_source,
            new_idf, new_france, new_remote, new_outside)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            stats.get("fetched", 0), stats.get("kept", 0), stats.get("new", 0),
            stats.get("filtered_new", 0), json.dumps(stats.get("new_by_source", {})),
            stats.get("new_idf", 0), stats.get("new_france", 0),
            stats.get("new_remote", 0), stats.get("new_outside", 0),
        ),
    )
    return cur.lastrowid


def list_jobs(conn: sqlite3.Connection, status: str | None = None, min_score: int = 0,
              filtered: int | None = 0, dismissed: bool | None = False) -> list[sqlite3.Row]:
    """filtered=0 (default) shows the main list, filtered=1 the auto-hidden bucket,
    filtered=None shows both. dismissed=False (default) hides jobs you rejected,
    dismissed=True shows only those, dismissed=None ignores the label."""
    q = """
        SELECT j.*, a.status, a.notes, a.submitted_url, a.cover_letter_path,
               (SELECT pdf_path FROM cv_artifacts c WHERE c.job_id = j.id
                ORDER BY c.generated_at DESC LIMIT 1) AS cv_pdf
        FROM jobs j JOIN applications a ON a.job_id = j.id
        WHERE j.score >= ?
    """
    params: list = [min_score]
    if filtered is not None:
        q += " AND COALESCE(j.filtered, 0) = ?"
        params.append(filtered)
    if dismissed is True:
        q += " AND COALESCE(j.user_label, '') = 'dismissed'"
    elif dismissed is False:
        q += " AND COALESCE(j.user_label, '') != 'dismissed'"
    if status:
        q += " AND a.status = ?"
        params.append(status)
    q += " ORDER BY j.score DESC, j.fetched_at DESC"
    return conn.execute(q, params).fetchall()


def set_filtered(conn: sqlite3.Connection, job_id: int, filtered: bool, reason: str = "") -> None:
    conn.execute(
        "UPDATE jobs SET filtered = ?, filter_reason = ? WHERE id = ?",
        (int(filtered), reason, job_id),
    )


def set_seniority(conn: sqlite3.Connection, job_id: int, seniority: str, min_years: int | None) -> None:
    conn.execute(
        "UPDATE jobs SET seniority = ?, min_years = ? WHERE id = ?",
        (seniority, min_years, job_id),
    )


def filtered_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(filtered, 0) = 1 "
        "AND COALESCE(user_label, '') != 'dismissed'"
    ).fetchone()[0]


FEEDBACK_LABELS = ("interested", "dismissed", "")
# Fixed vocabulary so Phase-4 learning can aggregate reasons deterministically.
DISMISS_REASONS = ["too_senior", "wrong_domain", "location", "contract", "company",
                   "stack_mismatch", "seniority_ok_but_weak", "other"]


def set_feedback(conn: sqlite3.Connection, job_id: int, label: str, reasons: str = "") -> None:
    """Record your explicit judgment. Marking a job 'interested' also rescues it from
    the auto-Filtered bucket (an explicit positive overrides the heuristic)."""
    if label not in FEEDBACK_LABELS:
        raise ValueError(f"unknown feedback label: {label}")
    if label == "dismissed":
        conn.execute(
            "UPDATE jobs SET user_label = 'dismissed', dismiss_reasons = ?, "
            "labeled_at = datetime('now') WHERE id = ?",
            (reasons, job_id),
        )
    elif label == "interested":
        conn.execute(
            "UPDATE jobs SET user_label = 'interested', dismiss_reasons = '', "
            "filtered = 0, filter_reason = '', labeled_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
    else:
        conn.execute(
            "UPDATE jobs SET user_label = '', dismiss_reasons = '', labeled_at = '' WHERE id = ?",
            (job_id,),
        )


def dismissed_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label, '') = 'dismissed'"
    ).fetchone()[0]


def labeled_jobs(conn: sqlite3.Connection, label: str) -> list[sqlite3.Row]:
    """All jobs carrying an explicit label ('interested' or 'dismissed'), for learning."""
    return conn.execute(
        "SELECT * FROM jobs WHERE COALESCE(user_label, '') = ? ORDER BY labeled_at DESC",
        (label,),
    ).fetchall()


# Statuses that count as "you engaged with this job" for lazy enrichment.
_ENGAGED_STATUSES = ("shortlisted", "cv_ready", "applied", "responded", "interview", "offer")


def set_description(conn: sqlite3.Connection, job_id: int, text: str) -> None:
    conn.execute(
        "UPDATE jobs SET description = ?, description_full = 1 WHERE id = ?",
        (text, job_id),
    )


def jobs_needing_enrichment(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Engaged jobs (interested, or moved past 'new') whose description is not yet full."""
    marks = ",".join("?" for _ in _ENGAGED_STATUSES)
    return conn.execute(
        f"""SELECT j.id, j.source, j.external_id, j.url FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE COALESCE(j.description_full, 0) = 0
              AND (COALESCE(j.user_label, '') = 'interested' OR a.status IN ({marks}))
            ORDER BY j.labeled_at DESC, j.fetched_at DESC
            LIMIT ?""",
        (*_ENGAGED_STATUSES, limit),
    ).fetchall()


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


def job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        source=row["source"], external_id=row["external_id"], title=row["title"],
        company=row["company"], location=row["location"], language=row["language"],
        url=row["url"], description=row["description"], contract_type=row["contract_type"],
        posted_at=row["posted_at"],
    )


def add_cv_artifact(conn: sqlite3.Connection, job_id: int, tex_path: str,
                    pdf_path: str, base_version: str = "") -> None:
    conn.execute(
        "INSERT INTO cv_artifacts (job_id, tex_path, pdf_path, base_version) VALUES (?,?,?,?)",
        (job_id, tex_path, pdf_path, base_version),
    )


def set_llm_judgment(conn: sqlite3.Connection, job_id: int, score: int,
                     verdict: str, reasons: str) -> None:
    conn.execute(
        "UPDATE jobs SET llm_score = ?, llm_verdict = ?, llm_reasons = ? WHERE id = ?",
        (score, verdict, reasons, job_id),
    )


def set_cover_letter(conn: sqlite3.Connection, job_id: int, path: str) -> None:
    conn.execute(
        "UPDATE applications SET cover_letter_path = ? WHERE job_id = ?",
        (path, job_id),
    )


RULE_KINDS = ("negative_kw", "company_block")


def add_rule(conn: sqlite3.Connection, kind: str, value: str, source: str = "learned",
             weight: int = 15, evidence: str = "", active: int = 0) -> bool:
    """Insert a rule. Learned rules default inactive (approval required). Returns True
    if newly inserted, False if a rule with the same (kind, value) already existed."""
    if kind not in RULE_KINDS:
        raise ValueError(f"unknown rule kind: {kind}")
    cur = conn.execute(
        """INSERT OR IGNORE INTO filter_rules (kind, value, source, weight, evidence, active)
           VALUES (?,?,?,?,?,?)""",
        (kind, value.lower().strip(), source, weight, evidence, int(active)),
    )
    return cur.rowcount > 0


def list_rules(conn: sqlite3.Connection, active: int | None = None,
               source: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM filter_rules WHERE 1=1"
    params: list = []
    if active is not None:
        q += " AND active = ?"
        params.append(active)
    if source is not None:
        q += " AND source = ?"
        params.append(source)
    q += " ORDER BY active DESC, hit_count DESC, id DESC"
    return conn.execute(q, params).fetchall()


def active_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM filter_rules WHERE active = 1").fetchall()


def set_rule_active(conn: sqlite3.Connection, rule_id: int, active: int) -> None:
    conn.execute("UPDATE filter_rules SET active = ? WHERE id = ?", (int(active), rule_id))


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,))


def bump_rule_hits(conn: sqlite3.Connection, rule_id: int, n: int = 1) -> None:
    conn.execute("UPDATE filter_rules SET hit_count = hit_count + ? WHERE id = ?", (n, rule_id))


def add_profile(conn: sqlite3.Connection, text: str, n_pos: int, n_neg: int) -> int:
    cur = conn.execute(
        "INSERT INTO preference_profile (text, n_pos, n_neg) VALUES (?,?,?)",
        (text, n_pos, n_neg),
    )
    return cur.lastrowid


def current_profile(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent preference profile, or None."""
    return conn.execute(
        "SELECT * FROM preference_profile ORDER BY id DESC LIMIT 1"
    ).fetchone()


def false_negative_stats(conn: sqlite3.Connection) -> dict:
    """Calibration signal: of the jobs you marked interested, how many had been
    auto-filtered? A high rate means the screen is too aggressive."""
    interested = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label,'') = 'interested'"
    ).fetchone()[0]
    fn = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label,'') = 'interested' "
        "AND COALESCE(was_filtered,0) = 1"
    ).fetchone()[0]
    dismissed_in_main = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label,'') = 'dismissed' "
        "AND COALESCE(was_filtered,0) = 0"
    ).fetchone()[0]
    return {
        "interested": interested,
        "false_negatives": fn,
        "false_negative_rate": round(fn / interested, 2) if interested else 0.0,
        "dismissed_escaped_screen": dismissed_in_main,
    }


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
