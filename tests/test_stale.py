from jobhunter import db
from jobhunter.models import Job


def J(ext, loc="Paris", title="ML Engineer"):
    return Job(source="wttj", external_id=ext, title=title, company="Acme",
               location=loc, url=f"http://x/{ext}")


def _set_seen(conn, job_id, last_seen):
    conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (last_seen, job_id))


def test_stale_relative_to_latest_run(tmp_db):
    with db.connect() as conn:
        fresh, _ = db.upsert_job(conn, J("1", title="ML Engineer A"), 60, "r")
        ghost, _ = db.upsert_job(conn, J("2", title="ML Engineer B"), 60, "r")
        # Latest fetch run is "now"; the ghost was last seen 30 days before it.
        conn.execute("INSERT INTO fetch_runs (ran_at) VALUES ('2026-02-01 00:00:00')")
        _set_seen(conn, fresh, "2026-02-01 00:00:00")
        _set_seen(conn, ghost, "2026-01-01 00:00:00")

        rows = {r["id"]: r for r in db.list_jobs(conn, filtered=None, staleness_days=14)}
        assert rows[fresh]["is_stale"] == 0
        assert rows[ghost]["is_stale"] == 1
        assert rows[ghost]["days_since_seen"] == 31
        assert db.stale_count(conn, 14) == 1


def test_not_stale_without_any_fetch_run(tmp_db):
    # No fetch_runs yet -> MAX(ran_at) is NULL -> nothing is stale (avoids false alarms).
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1"), 60, "r")
        _set_seen(conn, jid, "2020-01-01 00:00:00")
        row = db.list_jobs(conn, filtered=None)[0]
        assert row["is_stale"] == 0
        assert db.stale_count(conn) == 0


def test_threshold_is_configurable(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1"), 60, "r")
        conn.execute("INSERT INTO fetch_runs (ran_at) VALUES ('2026-02-01 00:00:00')")
        _set_seen(conn, jid, "2026-01-25 00:00:00")   # 7 days behind latest run
        assert db.list_jobs(conn, filtered=None, staleness_days=14)[0]["is_stale"] == 0
        assert db.list_jobs(conn, filtered=None, staleness_days=5)[0]["is_stale"] == 1
