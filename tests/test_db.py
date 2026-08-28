from jobhunter import db
from jobhunter.models import Job


def J(ext_id, title="ML Engineer", url="", company="Acme", loc="Paris"):
    return Job(source="wttj", external_id=ext_id, title=title, company=company,
               location=loc, url=url)


def test_upsert_and_status(tmp_db):
    with db.connect() as conn:
        jid, is_new = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        assert is_new is True
        row = db.get_job(conn, jid)
        assert row["status"] == "new"
        db.update_status(conn, jid, "shortlisted")
        assert db.get_job(conn, jid)["status"] == "shortlisted"


def test_content_dedup_same_url_different_id(tmp_db):
    # WTTJ reposts the same job under different objectIDs but the same URL.
    with db.connect() as conn:
        _, new1 = db.upsert_job(conn, J("1", url="http://x/job"), 60, "r")
        _, new2 = db.upsert_job(conn, J("2", url="http://x/job"), 60, "r")
        assert new1 is True and new2 is False
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_status_preserved_on_refetch(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "applied")
    with db.connect() as conn:
        db.upsert_job(conn, J("1", url="http://x/1"), 90, "new-reasons")
        row = db.get_job(conn, jid)
        assert row["status"] == "applied"      # status survives re-fetch
        assert row["score"] == 90              # score refreshed


def test_filtered_bucket_and_restore(tmp_db):
    with db.connect() as conn:
        main, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        hidden, _ = db.upsert_job(conn, J("2", url="http://x/2"), 55, "r",
                                  filtered=True, filter_reason="senior title", seniority="senior")
        assert [r["id"] for r in db.list_jobs(conn)] == [main]            # default: main only
        assert [r["id"] for r in db.list_jobs(conn, filtered=1)] == [hidden]
        assert len(db.list_jobs(conn, filtered=None)) == 2               # both
        assert db.filtered_count(conn) == 1

        db.set_filtered(conn, hidden, False)
        assert db.filtered_count(conn) == 0
        assert {r["id"] for r in db.list_jobs(conn)} == {main, hidden}


def test_feedback_dismiss_hides_from_main(tmp_db):
    with db.connect() as conn:
        keep, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        drop, _ = db.upsert_job(conn, J("2", url="http://x/2"), 60, "r")
        db.set_feedback(conn, drop, "dismissed", "too_senior,location")
        assert [r["id"] for r in db.list_jobs(conn)] == [keep]          # dismissed hidden
        dismissed = db.list_jobs(conn, filtered=None, dismissed=True)
        assert [r["id"] for r in dismissed] == [drop]
        assert dismissed[0]["dismiss_reasons"] == "too_senior,location"
        assert db.dismissed_count(conn) == 1
        assert [r["id"] for r in db.labeled_jobs(conn, "dismissed")] == [drop]


def test_interested_rescues_from_filtered(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 55, "r",
                               filtered=True, filter_reason="senior title", seniority="senior")
        assert db.filtered_count(conn) == 1
        db.set_feedback(conn, jid, "interested")
        assert db.filtered_count(conn) == 0                              # rescued
        row = db.get_job(conn, jid)
        assert row["filtered"] == 0 and row["user_label"] == "interested"
        db.set_feedback(conn, jid, "")                                   # clear
        assert db.get_job(conn, jid)["user_label"] == ""


def test_enrichment_selection_and_set_description(tmp_db):
    with db.connect() as conn:
        idle, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")   # new, untouched
        want, _ = db.upsert_job(conn, J("2", url="http://x/2"), 60, "r")
        db.set_feedback(conn, want, "interested")                          # engaged via label
        short, _ = db.upsert_job(conn, J("3", url="http://x/3"), 60, "r")
        db.update_status(conn, short, "shortlisted")                       # engaged via status

        assert {r["id"] for r in db.jobs_needing_enrichment(conn)} == {want, short}

        db.set_description(conn, want, "x" * 300)
        assert {r["id"] for r in db.jobs_needing_enrichment(conn)} == {short}   # want now full
        assert db.get_job(conn, want)["description_full"] == 1


def test_llm_and_cover_setters(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, jid, 88, "strong", "great fit")
        db.set_cover_letter(conn, jid, "/tmp/cl.md")
        rows = db.list_jobs(conn)
        assert rows[0]["llm_score"] == 88
        assert rows[0]["cover_letter_path"] == "/tmp/cl.md"
