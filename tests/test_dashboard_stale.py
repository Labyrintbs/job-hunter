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


def test_dashboard_shows_failed_cv_reason_and_review_badge(tmp_db):
    with db.connect() as conn:
        failed, _ = db.upsert_job(conn, J("10", title="Failed CV Job"), 60, "r")
        db.add_cv_artifact(conn, failed, "/tmp/cv.tex", "", note="compiled to 3 page(s)")
        sparse, _ = db.upsert_job(conn, J("11", title="Sparse CV Job"), 60, "r")
        db.add_cv_artifact(conn, sparse, "/tmp/cv.tex", "/tmp/cv.pdf", note="page 2 sparse (20% of page 1)")

    html = TestClient(app).get("/").text

    assert 'title="compiled to 3 page(s)">⚠ CV failed' in html
    assert 'title="page 2 sparse (20% of page 1)">⚠ review' in html


def test_dashboard_flags_a_failed_retailor_next_to_the_older_good_cv(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("12", title="Retailored Job"), 60, "r")
        db.add_cv_artifact(conn, jid, "/tmp/cv-1.tex", "/tmp/cv-1.pdf")
        db.add_cv_artifact(conn, jid, "/tmp/cv-2.tex", "", note="LaTeX compile error -- see cv-2.compile.log")

    html = TestClient(app).get("/").text

    assert "⚠ re-tailor failed" in html
    assert 'href="file:///tmp/cv-1.pdf"' in html      # the earlier good CV stays linked


def test_stuck_pill_shows_unenrichable_jobs_even_when_filtered_or_dismissed(tmp_db):
    with db.connect() as conn:
        filtered_id, _ = db.upsert_job(conn, J("1", title="Filtered No-JD Job"), 60, "r")
        db.set_filtered(conn, filtered_id, True, "requires 5+ yrs")
        dismissed_id, _ = db.upsert_job(conn, J("2", title="Dismissed No-JD Job"), 60, "r")
        db.set_feedback(conn, dismissed_id, "dismissed")
        conn.execute(
            "UPDATE jobs SET description_full=0, enrich_attempts=? WHERE id IN (?, ?)",
            (db.MAX_ENRICH_ATTEMPTS, filtered_id, dismissed_id),
        )
        not_yet_tried_id, _ = db.upsert_job(conn, J("3", title="Fresh Untried Job"), 60, "r")
        conn.execute("UPDATE jobs SET description_full=0, enrich_attempts=0 WHERE id=?",
                     (not_yet_tried_id,))

    client = TestClient(app)
    resp = client.get("/", params={"stuck": 1})

    assert resp.status_code == 200
    assert "Filtered No-JD Job" in resp.text
    assert "Dismissed No-JD Job" in resp.text
    assert "Fresh Untried Job" not in resp.text   # only exhausted retries, not just untried


def test_dashboard_shows_the_cv_language_and_a_switch_to_the_other_one(tmp_db):
    with db.connect() as conn:
        fr, _ = db.upsert_job(conn, J("20", title="French CV Job"), 60, "r")
        db.add_cv_artifact(conn, fr, "/tmp/cv-fr.tex", "/tmp/cv-fr.pdf", lang="fr")
        en, _ = db.upsert_job(conn, J("21", title="English CV Job"), 60, "r")
        db.add_cv_artifact(conn, en, "/tmp/cv-en.tex", "/tmp/cv-en.pdf", lang="en")
        manual, _ = db.upsert_job(conn, J("22", title="Hand Revised Job"), 60, "r")
        db.add_cv_artifact(conn, manual, "", "/tmp/rev.pdf", origin="revised")

    html = TestClient(app).get("/").text

    assert f"tailorCV(this, {fr}, 'en')" in html and "→EN" in html          # a French CV offers English
    assert f"tailorCV(this, {en}, 'fr')" in html and "→FR" in html          # an English CV offers French
    assert f"tailorCV(this, {manual}, 'fr')" not in html and f"tailorCV(this, {manual}, 'en')" not in html
    assert ">FR</span>" in html and ">EN</span>" in html


def test_review_pill_counts_and_lists_cvs_with_a_note(tmp_db):
    with db.connect() as conn:
        noted, _ = db.upsert_job(conn, J("30", title="Noted CV Job"), 60, "r")
        db.add_cv_artifact(conn, noted, "/tmp/a.tex", "/tmp/a.pdf",
                           note="summary fell back to the standard text (language: 3 English function words)")
        clean, _ = db.upsert_job(conn, J("31", title="Clean CV Job"), 60, "r")
        db.add_cv_artifact(conn, clean, "/tmp/b.tex", "/tmp/b.pdf")
        db.upsert_job(conn, J("32", title="No CV Job"), 60, "r")
        assert db.review_count(conn) == 1

    client = TestClient(app)
    assert "CV review · 1" in client.get("/").text
    listed = client.get("/", params={"review": 1}).text
    assert "Noted CV Job" in listed and "Clean CV Job" not in listed and "No CV Job" not in listed


def test_a_newer_clean_cv_clears_the_review_count(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("40"), 60, "r")
        db.add_cv_artifact(conn, jid, "/tmp/a.tex", "/tmp/a.pdf", note="language check: ...")
        assert db.review_count(conn) == 1
        db.add_cv_artifact(conn, jid, "/tmp/b.tex", "/tmp/b.pdf")
        assert db.review_count(conn) == 0
