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
    assert f'href="/cv/{jid}.pdf"' in html            # the earlier good CV stays linked


def test_cv_and_letter_links_are_served_over_http_from_the_data_dir_only(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr("jobhunter.web.app.DATA_DIR", tmp_path)
    good_pdf, good_letter, outside = tmp_path / "cv.pdf", tmp_path / "cover_letter.md", tmp_path.parent / "secret.pdf"
    good_pdf.write_bytes(b"%PDF-1.4 mine")
    good_letter.write_text("Dear team")
    outside.write_bytes(b"%PDF-1.4 secret")
    with db.connect() as conn:
        ok, _ = db.upsert_job(conn, J("50", title="Has Docs"), 60, "r")
        db.add_cv_artifact(conn, ok, "", str(good_pdf))
        conn.execute("UPDATE applications SET cover_letter_path = ? WHERE job_id = ?", (str(good_letter), ok))
        bad, _ = db.upsert_job(conn, J("51", title="Path Outside"), 60, "r")
        db.add_cv_artifact(conn, bad, "", str(outside))
        none, _ = db.upsert_job(conn, J("52", title="No Docs"), 60, "r")

    client = TestClient(app)
    pdf, letter = client.get(f"/cv/{ok}.pdf"), client.get(f"/letter/{ok}")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    assert letter.status_code == 200 and letter.text == "Dear team"
    assert client.get(f"/cv/{bad}.pdf").status_code == 404      # stored path outside data/ is never served
    assert client.get(f"/cv/{none}.pdf").status_code == 404 and client.get(f"/letter/{none}").status_code == 404


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


def test_dashboard_shows_the_cv_date_and_flags_a_cv_older_than_the_master(tmp_db, monkeypatch):
    monkeypatch.setattr("jobhunter.web.app.master_edited_at", lambda lang: "2026-10-01 10:00:00")
    with db.connect() as conn:
        old, _ = db.upsert_job(conn, J("40", title="Old Master Job"), 60, "r")
        db.add_cv_artifact(conn, old, "/tmp/o.tex", "/tmp/o.pdf", lang="en")
        new, _ = db.upsert_job(conn, J("41", title="Fresh Master Job"), 60, "r")
        db.add_cv_artifact(conn, new, "/tmp/n.tex", "/tmp/n.pdf", lang="fr")
        revised, _ = db.upsert_job(conn, J("42", title="Hand Revised Old Job"), 60, "r")
        db.add_cv_artifact(conn, revised, "", "/tmp/r.pdf", origin="revised")
        conn.execute("UPDATE cv_artifacts SET generated_at = '2026-09-30 08:00:00' WHERE job_id IN (?, ?)",
                     (old, revised))
        conn.execute("UPDATE cv_artifacts SET generated_at = '2026-10-01 12:00:00' WHERE job_id = ?", (new,))

    html = TestClient(app).get("/").text

    assert html.count(">old master</span>") == 1       # only the AI CV from before the master's edit
    assert "09-30" in html and "10-01" in html          # generation dates shown


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
