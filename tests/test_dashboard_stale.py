from fastapi.testclient import TestClient

from jobhunter import db
from jobhunter.models import Job
from jobhunter.web.app import app


def J(ext, title="ML Engineer"):
    return Job(source="wttj", external_id=ext, title=title, company="Acme",
               location="Paris", url=f"http://x/{ext}")


def test_stale_pill_shows_stale_jobs_even_when_filtered_or_dismissed(tmp_db):
    # Regression: clicking "stale" ran the default main-list query first (which hides
    # filtered/dismissed jobs), then filtered *that* down to stale ones -- so a stale
    # job sitting in the Filtered bucket never showed up even though the "stale · N"
    # count badge (unrestricted) said it existed.
    with db.connect() as conn:
        filtered_id, _ = db.upsert_job(conn, J("1", title="Filtered Stale Job"), 60, "r")
        db.set_filtered(conn, filtered_id, True, "requires 5+ yrs")
        dismissed_id, _ = db.upsert_job(conn, J("2", title="Dismissed Stale Job"), 60, "r")
        db.set_feedback(conn, dismissed_id, "dismissed")
        conn.execute("INSERT INTO fetch_runs (ran_at) VALUES ('2026-02-01 00:00:00')")
        conn.execute(
            "UPDATE jobs SET last_seen = '2026-01-01 00:00:00' WHERE id IN (?, ?)",
            (filtered_id, dismissed_id),
        )

    client = TestClient(app)
    resp = client.get("/", params={"stale": 1})

    assert resp.status_code == 200
    assert "Filtered Stale Job" in resp.text
    assert "Dismissed Stale Job" in resp.text
