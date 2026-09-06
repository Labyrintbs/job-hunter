from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from difflib import SequenceMatcher
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
    "unavailable",
]

EVENT_TYPES = ("created", "status", "filtered", "label")

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
    origin       TEXT DEFAULT 'ai',      -- base | ai | revised (human-uploaded)
    generated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,                -- created | status | filtered | label
    from_value  TEXT DEFAULT '',
    to_value    TEXT DEFAULT '',
    detail      TEXT DEFAULT '',               -- filter_reason / dismiss_reasons, optional
    source      TEXT NOT NULL DEFAULT 'live',  -- live | backfill
    occurred_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id);
CREATE INDEX IF NOT EXISTS idx_job_events_type_time ON job_events(event_type, occurred_at);

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
        "interested_reasons": "TEXT DEFAULT ''",
        "labeled_at": "TEXT DEFAULT ''",
        "description_full": "INTEGER DEFAULT 0",
        "was_filtered": "INTEGER DEFAULT 0",
        "geo_tier": "TEXT DEFAULT ''",
        "last_seen": "TEXT DEFAULT ''",
        "role_category": "TEXT DEFAULT ''",
    },
    "applications": {
        "cover_letter_path": "TEXT DEFAULT ''",
    },
    "cv_artifacts": {
        "origin": "TEXT DEFAULT 'ai'",
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


def _backfill_role_category(conn: sqlite3.Connection) -> None:
    """Populate role_category for pre-existing rows (new rows get it at upsert time)."""
    rows = conn.execute(
        "SELECT id, title, description FROM jobs WHERE COALESCE(role_category,'') = ''"
    ).fetchall()
    if not rows:
        return
    from . import match
    from .config import load_search_config
    try:
        config = load_search_config()
    except Exception:
        config = {}
    for r in rows:
        cat = match.classify_role(r["title"], r["description"], config)
        conn.execute("UPDATE jobs SET role_category = ? WHERE id = ?", (cat, r["id"]))


def _backfill_job_events(conn: sqlite3.Connection) -> None:
    """Synthesize job_events for jobs that predate this feature, from currently
    available timestamps only (fetched_at, applications.updated_at, labeled_at).
    Idempotent: only touches job_ids with zero existing job_events rows -- once the
    write-site hooks are live, every job gets at least one row on its next write, so
    "zero rows" can only mean "predates this feature". Safe to call on every startup.

    Known limitation: intermediate status hops aren't reconstructable (updated_at only
    ever held the *latest* transition), so a job now 'applied' that actually passed
    through shortlisted/cv_ready first collapses into one synthetic 'new'->'applied'
    event. Similarly, a currently-unfiltered job that was once filtered but never
    explicitly rescued via 'interested' has no recoverable un-filter timestamp and is
    left alone."""
    rows = conn.execute(
        """SELECT j.id, j.fetched_at, j.filtered, j.was_filtered, j.user_label, j.labeled_at,
                  a.status, a.updated_at
           FROM jobs j JOIN applications a ON a.job_id = j.id
           WHERE NOT EXISTS (SELECT 1 FROM job_events e WHERE e.job_id = j.id)"""
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO job_events (job_id, event_type, from_value, to_value, source, occurred_at) "
            "VALUES (?, 'created', '', 'new', 'backfill', ?)", (r["id"], r["fetched_at"]))
        if r["status"] != "new":
            conn.execute(
                "INSERT INTO job_events (job_id, event_type, from_value, to_value, source, occurred_at) "
                "VALUES (?, 'status', 'new', ?, 'backfill', ?)",
                (r["id"], r["status"], r["updated_at"] or r["fetched_at"]))
        if r["filtered"]:
            conn.execute(
                "INSERT INTO job_events (job_id, event_type, from_value, to_value, detail, source, occurred_at) "
                "VALUES (?, 'filtered', '0', '1', 'backfilled: original reason unknown', 'backfill', ?)",
                (r["id"], r["fetched_at"]))
        if r["user_label"]:
            label_time = r["labeled_at"] or r["fetched_at"]
            conn.execute(
                "INSERT INTO job_events (job_id, event_type, from_value, to_value, source, occurred_at) "
                "VALUES (?, 'label', '', ?, 'backfill', ?)", (r["id"], r["user_label"], label_time))
            if r["user_label"] == "interested" and r["was_filtered"] and not r["filtered"]:
                conn.execute(
                    "INSERT INTO job_events (job_id, event_type, from_value, to_value, detail, source, occurred_at) "
                    "VALUES (?, 'filtered', '1', '0', 'backfilled: rescued by interested label', 'backfill', ?)",
                    (r["id"], label_time))


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _backfill_geo_tier(conn)
        _backfill_role_category(conn)
        _backfill_job_events(conn)


@contextmanager
def connect(db_path: Path | None = None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _normalize(s: str) -> str:
    """Casefold + strip accents/punctuation so the same posting's text compares equal
    across sources even when they render it slightly differently (accents, spacing,
    "Île-de-France" vs "Ile-de-France")."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _norm_city(location: str) -> str:
    """Just the city, normalized. Sources format the full location wildly differently
    for the same posting -- WTTJ: "Paris, Ile-de-France, France", an ATS board: "Paris",
    LinkedIn: "Paris, Île-de-France" -- so matching the full string never lines up across
    sources. The city (first comma-separated segment) is the one part they share.
    HelloWork has no comma at all -- it appends a trailing department code instead
    ("Paris - 75"), which without stripping would normalize to "paris 75" and never
    match another source's bare "paris" for the same city."""
    city = (location or "").split(",")[0]
    city = re.sub(r"\s*-\s*\d+\s*$", "", city)
    return _normalize(city)


_TITLE_JUNK_TOKENS = {"h", "f", "m", "w", "d", "nb", "cdi", "cdd", "stage"}


def _strip_title_junk(norm_title: str) -> str:
    """Drop gender/contract boilerplate tokens (H/F, F/M/D, CDI, ...) that inflate or
    deflate a title's apparent difference without carrying any real signal."""
    return " ".join(w for w in norm_title.split() if w not in _TITLE_JUNK_TOKENS)


def _companies_related(a: str, b: str) -> bool:
    """True if one normalized company name contains the other, but they aren't
    identical -- catches a parent/subsidiary naming drift across sources (e.g.
    HelloWork's "Ubisoft" vs LinkedIn's "Ubisoft Paris Studio" for the same posting).
    Deliberately excludes an exact match: at the exact same employer, a near-identical
    title is far more often two genuinely different open roles (different squad,
    seniority level, or specialization) than a stray duplicate -- title-similarity
    ratios for those two cases overlap too heavily (tested against this DB's real
    data) to separate with a threshold, so the company axis has to do the work here."""
    na, nb = _normalize(a), _normalize(b)
    if na == nb or len(na) < 4 or len(nb) < 4:
        return False
    return na in nb or nb in na


def find_possible_duplicates(conn: sqlite3.Connection, title_ratio: float = 0.80) -> list[dict]:
    """Non-destructive 'maybe the same posting' detector: same city + related company
    names + near-identical title (after stripping boilerplate). Deliberately NOT used
    to auto-merge -- company-name relatedness alone is too risky to silently collapse
    (a conglomerate like VINCI has genuinely separate subsidiaries hiring
    independently), so this only surfaces candidates for a human to judge. Title
    similarity alone is also not a safe signal on its own (many genuinely different
    employers share a generic title like "AI Engineer"), which is why both gates are
    required together. Uses only the stdlib (difflib) -- no embeddings needed at this
    scale (a few hundred rows)."""
    rows = conn.execute("SELECT id, company, title, location FROM jobs").fetchall()
    by_city: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for r in rows:
        title = _strip_title_junk(_normalize(r["title"]))
        if title:
            by_city[_norm_city(r["location"])].append((r["id"], r["company"], title))

    pairs = []
    for bucket in by_city.values():
        n = len(bucket)
        for i in range(n):
            id_a, co_a, ta = bucket[i]
            for k in range(i + 1, n):
                id_b, co_b, tb = bucket[k]
                if not _companies_related(co_a, co_b):
                    continue
                if SequenceMatcher(None, ta, tb).ratio() >= title_ratio:
                    pairs.append({"a": id_a, "b": id_b})
    return pairs


def possible_duplicates_map(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """job_id -> ids of its likely duplicates, both directions, for O(1) template lookup."""
    m: dict[int, list[int]] = defaultdict(list)
    for p in find_possible_duplicates(conn):
        m[p["a"]].append(p["b"])
        m[p["b"]].append(p["a"])
    return dict(m)


def _find_content_match(conn: sqlite3.Connection, job: Job) -> sqlite3.Row | None:
    """Cross-source dedup: same normalized company + title + city, any source (different
    platforms give the same real posting different URLs and IDs, so those can't be the
    match key here). SQL narrows to same company first (cheap, uses no accent folding)
    before the accent-aware Python comparison on the small remainder."""
    norm_title = _normalize(job.title)
    norm_city = _norm_city(job.location)
    candidates = conn.execute(
        "SELECT id, title, location FROM jobs WHERE LOWER(company) = LOWER(?)",
        (job.company,),
    ).fetchall()
    for c in candidates:
        if _normalize(c["title"]) == norm_title and _norm_city(c["location"]) == norm_city:
            return c
    return None


def _log_event(conn: sqlite3.Connection, job_id: int, event_type: str,
                from_value, to_value, detail: str = "", source: str = "live") -> None:
    """Record one job_events row. Skips true no-op transitions (old == new) so a
    write site that re-asserts the same value never pollutes the timeline."""
    from_s = "" if from_value is None else str(from_value)
    to_s = "" if to_value is None else str(to_value)
    if from_s == to_s:
        return
    conn.execute(
        "INSERT INTO job_events (job_id, event_type, from_value, to_value, detail, source) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, event_type, from_s, to_s, detail, source),
    )


def upsert_job(conn: sqlite3.Connection, job: Job, score: int, reasons: str, *,
               filtered: bool = False, filter_reason: str = "",
               seniority: str = "", min_years: int | None = None,
               geo_tier: str = "", role_category: str = "") -> tuple[int, bool]:
    """Insert a job if new. Returns (job_id, is_new). Existing jobs keep their
    application status; their score/reasons and screening flags are refreshed, and
    last_seen is stamped every time the job is seen (for staleness / market signals).
    Dedups on (source, external_id), then on exact URL, then cross-source on normalized
    (company, title, location) — the same posting fetched from WTTJ, a company's own ATS
    board, and LinkedIn all land on one row instead of three."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
        (job.source, job.external_id),
    ).fetchone()
    if not row and job.url:
        row = conn.execute(
            "SELECT id FROM jobs WHERE url = ? AND url != ''", (job.url,)
        ).fetchone()
    if not row:
        row = _find_content_match(conn, job)
    if row:
        old = conn.execute("SELECT filtered FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        conn.execute(
            """UPDATE jobs SET score = ?, match_reasons = ?, filtered = ?,
               filter_reason = ?, seniority = ?, min_years = ?, geo_tier = ?,
               role_category = ?, last_seen = datetime('now'),
               was_filtered = CASE WHEN ? THEN 1 ELSE was_filtered END WHERE id = ?""",
            (score, reasons, int(filtered), filter_reason, seniority, min_years, geo_tier,
             role_category, int(filtered), row["id"]),
        )
        _log_event(conn, row["id"], "filtered", old["filtered"] if old else 0, int(filtered),
                   detail=filter_reason)
        return row["id"], False

    cur = conn.execute(
        """INSERT INTO jobs
           (source, external_id, title, company, location, language, url,
            description, contract_type, posted_at, score, match_reasons,
            filtered, filter_reason, seniority, min_years, description_full, was_filtered,
            geo_tier, role_category, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            job.source, job.external_id, job.title, job.company, job.location,
            job.language, job.url, job.description, job.contract_type,
            job.posted_at, score, reasons,
            int(filtered), filter_reason, seniority, min_years,
            int(len(job.description or "") > 200), int(filtered), geo_tier, role_category,
        ),
    )
    job_id = cur.lastrowid
    conn.execute("INSERT INTO applications (job_id, status) VALUES (?, 'new')", (job_id,))
    _log_event(conn, job_id, "created", "", "new")
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


SORT_ORDERS = {
    # Once the LLM judge has an opinion, it's a more informed signal than the rule-based
    # keyword score (which can't see company type, domain fit, or hard disqualifiers) --
    # let it take over ranking. Unjudged jobs fall back to the rule score, all we have.
    "score": "COALESCE(j.llm_score, j.score) DESC, j.score DESC, j.fetched_at DESC",
    "fetched_at": "j.fetched_at DESC, j.score DESC",
}


def list_jobs(conn: sqlite3.Connection, status: str | None = None, min_score: int = 0,
              filtered: int | None = 0, dismissed: bool | None = False,
              interested: bool | None = None,
              staleness_days: int = 14, exclude_statuses: tuple = (),
              sort: str = "score") -> list[sqlite3.Row]:
    """filtered=0 (default) shows the main list, filtered=1 the auto-hidden bucket,
    filtered=None shows both. dismissed=False (default) hides jobs you rejected,
    dismissed=True shows only those, dismissed=None ignores the label. interested
    mirrors dismissed but for the 'interested' label (default None: ignore it).
    exclude_statuses hides those statuses unless `status` names one of them.
    sort picks an entry from SORT_ORDERS ("score" or "fetched_at"), falling back to score.
    Adds computed `is_stale` / `days_since_seen` (relative to the latest fetch run)."""
    q = """
        SELECT j.*, a.status, a.notes, a.submitted_url, a.cover_letter_path, a.updated_at,
               (SELECT pdf_path FROM cv_artifacts c WHERE c.job_id = j.id
                ORDER BY c.generated_at DESC LIMIT 1) AS cv_pdf,
               (SELECT origin FROM cv_artifacts c WHERE c.job_id = j.id
                ORDER BY c.generated_at DESC LIMIT 1) AS cv_origin,
               (SELECT COUNT(*) FROM cv_artifacts c WHERE c.job_id = j.id) AS cv_versions,
               (SELECT GROUP_CONCAT(to_value || ' (' || substr(occurred_at, 1, 10) || ')', ' -> ')
                FROM job_events e WHERE e.job_id = j.id AND e.event_type IN ('created', 'status')
                ORDER BY e.occurred_at, e.id) AS status_timeline,
               CAST(julianday((SELECT MAX(ran_at) FROM fetch_runs))
                    - julianday(NULLIF(j.last_seen, '')) AS INTEGER) AS days_since_seen,
               CASE WHEN COALESCE(j.last_seen, '') <> ''
                     AND (julianday((SELECT MAX(ran_at) FROM fetch_runs))
                          - julianday(j.last_seen)) > ?
                    THEN 1 ELSE 0 END AS is_stale
        FROM jobs j JOIN applications a ON a.job_id = j.id
        WHERE j.score >= ?
    """
    params: list = [staleness_days, min_score]
    if filtered is not None:
        q += " AND COALESCE(j.filtered, 0) = ?"
        params.append(filtered)
    if dismissed is True:
        q += " AND COALESCE(j.user_label, '') = 'dismissed'"
    elif dismissed is False:
        q += " AND COALESCE(j.user_label, '') != 'dismissed'"
    if interested is True:
        q += " AND COALESCE(j.user_label, '') = 'interested'"
    elif interested is False:
        q += " AND COALESCE(j.user_label, '') != 'interested'"
    if status:
        q += " AND a.status = ?"
        params.append(status)
    elif exclude_statuses:
        marks = ",".join("?" for _ in exclude_statuses)
        q += f" AND a.status NOT IN ({marks})"
        params.extend(exclude_statuses)
    q += " ORDER BY " + SORT_ORDERS.get(sort, SORT_ORDERS["score"])
    return conn.execute(q, params).fetchall()


def jobs_by_id_needing_enrichment(conn: sqlite3.Connection, job_ids: list[int]) -> list[sqlite3.Row]:
    """Given a specific set of job ids (e.g. one run's freshly-inserted postings), those
    still lacking a real description -- independent of engagement. Backs the local JD
    corpus for every new posting, not just ones you've acted on."""
    if not job_ids:
        return []
    marks = ",".join("?" for _ in job_ids)
    return conn.execute(
        f"SELECT id, source, external_id, url FROM jobs "
        f"WHERE id IN ({marks}) AND COALESCE(description_full, 0) = 0",
        job_ids,
    ).fetchall()


def jobs_pending_enrichment_any(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Any job (regardless of engagement) still lacking a real description --
    a backlog-wide retry for enrichment that failed at fetch time, unlike
    jobs_needing_enrichment (gated to engaged jobs only). Skips filtered/
    dismissed jobs since there's no point enriching those. Oldest first."""
    return conn.execute(
        """SELECT id, source, external_id, url FROM jobs
           WHERE COALESCE(description_full, 0) = 0
             AND COALESCE(filtered, 0) = 0
             AND COALESCE(user_label, '') != 'dismissed'
           ORDER BY fetched_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def jobs_with_full_description(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every job that already has a full description stored -- used to backfill
    the local JD text files for jobs enriched before jd_store existed."""
    return conn.execute(
        "SELECT id, source, external_id, title, company, url, description FROM jobs "
        "WHERE COALESCE(description_full, 0) = 1"
    ).fetchall()


def update_screening(conn: sqlite3.Connection, job_id: int, score: int, reasons: str, *,
                     filtered: bool, filter_reason: str, seniority: str,
                     min_years: int | None, role_category: str = "") -> None:
    """Refresh score/screening after enrichment adds real JD content -- a job scored on
    title alone (LinkedIn cards, thin WTTJ profiles) becomes content-aware once the
    description lands, which can also flip its filtered bucket (e.g. a '5+ years'
    requirement only visible in the body) or its role category (e.g. a generic "ML
    Engineer" title that's actually NLP-focused per the body)."""
    old = conn.execute("SELECT filtered FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.execute(
        """UPDATE jobs SET score = ?, match_reasons = ?, filtered = ?, filter_reason = ?,
           seniority = ?, min_years = ?, role_category = ?,
           was_filtered = CASE WHEN ? THEN 1 ELSE was_filtered END WHERE id = ?""",
        (score, reasons, int(filtered), filter_reason, seniority, min_years, role_category,
         int(filtered), job_id),
    )
    _log_event(conn, job_id, "filtered", old["filtered"] if old else 0, int(filtered),
               detail=filter_reason)


def set_filtered(conn: sqlite3.Connection, job_id: int, filtered: bool, reason: str = "") -> None:
    old = conn.execute("SELECT filtered FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.execute(
        "UPDATE jobs SET filtered = ?, filter_reason = ? WHERE id = ?",
        (int(filtered), reason, job_id),
    )
    _log_event(conn, job_id, "filtered", old["filtered"] if old else 0, int(filtered), detail=reason)


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
    old = conn.execute("SELECT user_label, filtered FROM jobs WHERE id = ?", (job_id,)).fetchone()
    old_label = (old["user_label"] or "") if old else ""
    old_filtered = (old["filtered"] if old else 0) or 0
    if label == "dismissed":
        conn.execute(
            "UPDATE jobs SET user_label = 'dismissed', dismiss_reasons = ?, "
            "interested_reasons = '', labeled_at = datetime('now') WHERE id = ?",
            (reasons, job_id),
        )
        _log_event(conn, job_id, "label", old_label, "dismissed", detail=reasons)
    elif label == "interested":
        conn.execute(
            "UPDATE jobs SET user_label = 'interested', dismiss_reasons = '', "
            "interested_reasons = ?, filtered = 0, filter_reason = '', "
            "labeled_at = datetime('now') WHERE id = ?",
            (reasons, job_id),
        )
        _log_event(conn, job_id, "label", old_label, "interested", detail=reasons)
        _log_event(conn, job_id, "filtered", old_filtered, 0, detail="rescued by interested label")
    else:
        conn.execute(
            "UPDATE jobs SET user_label = '', dismiss_reasons = '', "
            "interested_reasons = '', labeled_at = '' WHERE id = ?",
            (job_id,),
        )
        _log_event(conn, job_id, "label", old_label, "")


def dismissed_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label, '') = 'dismissed'"
    ).fetchone()[0]


def interested_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(user_label, '') = 'interested'"
    ).fetchone()[0]


def last_fetch_at(conn: sqlite3.Connection) -> str | None:
    """UTC timestamp string of the most recent fetch_runs row, or None if never run."""
    return conn.execute("SELECT MAX(ran_at) FROM fetch_runs").fetchone()[0]


def stale_count(conn: sqlite3.Connection, staleness_days: int = 14) -> int:
    """Jobs not seen in `staleness_days` relative to the latest fetch run (likely delisted)."""
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(last_seen, '') <> '' "
        "AND (julianday((SELECT MAX(ran_at) FROM fetch_runs)) - julianday(last_seen)) > ?",
        (staleness_days,),
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
    old = conn.execute("SELECT status FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    conn.execute(
        "UPDATE applications SET status = ?, updated_at = datetime('now') WHERE job_id = ?",
        (status, job_id),
    )
    _log_event(conn, job_id, "status", old["status"] if old else "", status)


def job_from_row(row: sqlite3.Row) -> Job:
    """Reconstruct a Job from a stored row. The nullable TEXT columns are
    coalesced to "" -- Job's fields are non-optional strings, but a handful of
    old HelloWork-sourced rows have a genuine NULL description in the DB (a
    stale-data issue, not a live code path -- current hellowork.py never sets
    it), which would otherwise crash pydantic validation here."""
    return Job(
        source=row["source"], external_id=row["external_id"], title=row["title"],
        company=row["company"], location=row["location"] or "", language=row["language"] or "",
        url=row["url"] or "", description=row["description"] or "",
        contract_type=row["contract_type"] or "", posted_at=row["posted_at"] or "",
    )


def add_cv_artifact(conn: sqlite3.Connection, job_id: int, tex_path: str,
                    pdf_path: str, base_version: str = "", origin: str = "ai") -> int:
    """Append a CV artifact (append-only; latest-by-generated_at is the active one).
    origin: 'ai' (auto-tailored) | 'revised' (human-uploaded) | 'base'."""
    cur = conn.execute(
        "INSERT INTO cv_artifacts (job_id, tex_path, pdf_path, base_version, origin) VALUES (?,?,?,?,?)",
        (job_id, tex_path, pdf_path, base_version, origin),
    )
    return cur.lastrowid


def list_cv_artifacts(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """All CV versions for a job, newest first."""
    return conn.execute(
        "SELECT * FROM cv_artifacts WHERE job_id = ? ORDER BY generated_at DESC, id DESC",
        (job_id,),
    ).fetchall()


def jobs_ready_for_auto_tailor(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Backlog-wide: jobs the LLM judge rated strong/good/stretch that don't
    have a CV artifact yet. Unlike daily_run's inline auto-tailor gate (scoped
    to that run's freshly-judged jobs only), this sweeps every qualifying job
    ever judged, so one that missed a prior cutoff isn't stuck forever.
    Strongest fit first, in case the backlog exceeds the batch size."""
    return conn.execute(
        """SELECT id FROM jobs j
           WHERE llm_verdict IN ('strong', 'good', 'stretch')
             AND NOT EXISTS (SELECT 1 FROM cv_artifacts c WHERE c.job_id = j.id)
           ORDER BY llm_score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def set_llm_filter(conn: sqlite3.Connection, job_id: int, filter_reason: str) -> None:
    """Auto-hide a job into the Filtered bucket on a weak LLM verdict, without touching
    its rule-based score/reasons. Appends to any existing filter_reason rather than
    clobbering it, in case a rule-based reason is already there."""
    row = conn.execute("SELECT filter_reason, filtered FROM jobs WHERE id = ?", (job_id,)).fetchone()
    existing = (row["filter_reason"] or "").strip() if row else ""
    old_filtered = (row["filtered"] if row else 0) or 0
    combined = f"{existing}; {filter_reason}" if existing else filter_reason
    conn.execute(
        "UPDATE jobs SET filtered = 1, filter_reason = ? WHERE id = ?",
        (combined, job_id),
    )
    _log_event(conn, job_id, "filtered", old_filtered, 1, detail=filter_reason)


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


def job_event_history(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """Full ordered event history for one job (oldest first)."""
    return conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY occurred_at ASC, id ASC",
        (job_id,),
    ).fetchall()


def all_job_events(conn: sqlite3.Connection, event_type: str | None = None,
                   since: str | None = None) -> list[sqlite3.Row]:
    """All events, optionally filtered by type / since a timestamp, oldest first.
    Joins in title/company/source/score for readable tooltips."""
    q = ("SELECT e.*, j.title, j.company, j.source, j.score, j.llm_verdict "
         "FROM job_events e JOIN jobs j ON j.id = e.job_id WHERE 1=1")
    params: list = []
    if event_type:
        q += " AND e.event_type = ?"
        params.append(event_type)
    if since:
        q += " AND e.occurred_at >= ?"
        params.append(since)
    q += " ORDER BY e.occurred_at ASC, e.id ASC"
    return conn.execute(q, params).fetchall()


def status_transition_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(from_value, to_value, n) for every status event -- the Sankey's edge list."""
    return conn.execute(
        "SELECT from_value, to_value, COUNT(*) AS n FROM job_events "
        "WHERE event_type = 'status' GROUP BY from_value, to_value"
    ).fetchall()


def stage_reach_counts(conn: sqlite3.Connection, stages: tuple[str, ...]) -> list[sqlite3.Row]:
    """For each stage in `stages`, how many distinct jobs ever reached it (to_value =
    stage in a status event) -- the funnel's monotonic bar heights, unlike current
    applications.status which only shows where a job is *now*."""
    marks = ",".join("?" for _ in stages)
    return conn.execute(
        f"SELECT to_value AS stage, COUNT(DISTINCT job_id) AS n FROM job_events "
        f"WHERE event_type = 'status' AND to_value IN ({marks}) GROUP BY to_value",
        stages,
    ).fetchall()


def events_by_day(conn: sqlite3.Connection, event_type: str) -> list[sqlite3.Row]:
    """date(occurred_at), to_value, count -- backs the timeline chart."""
    return conn.execute(
        "SELECT date(occurred_at) AS day, to_value, COUNT(*) AS n FROM job_events "
        "WHERE event_type = ? GROUP BY day, to_value ORDER BY day",
        (event_type,),
    ).fetchall()
