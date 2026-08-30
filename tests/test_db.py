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


def test_cross_source_content_dedup(tmp_db):
    # Same posting, fetched from WTTJ and from the company's own Ashby board: different
    # source, different external_id, different URL, and each platform formats location
    # completely differently ("Paris, Ile-de-France, France" vs bare "Paris") -- still
    # one job, matched on normalized company + title + city.
    wttj = Job(source="wttj", external_id="w1", title="Machine Learning Engineer",
               company="Doctolib", location="Paris, Île-de-France, France",
               url="https://wttj.example/w1")
    ashby = Job(source="ashby", external_id="a1", title="machine   learning engineer",
                company="Doctolib", location="Paris", url="https://doctolib.example/a1")
    with db.connect() as conn:
        jid1, new1 = db.upsert_job(conn, wttj, 60, "r")
        jid2, new2 = db.upsert_job(conn, ashby, 65, "r2")
        assert new1 is True and new2 is False
        assert jid1 == jid2
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.get_job(conn, jid1)["score"] == 65   # refreshed by the second sighting


def test_cross_source_no_dedup_for_different_title(tmp_db):
    # Same company/location but a genuinely different role must stay separate.
    a = Job(source="wttj", external_id="1", title="Machine Learning Engineer",
            company="Acme", location="Paris", url="http://x/1")
    b = Job(source="lever", external_id="2", title="Backend Engineer",
            company="Acme", location="Paris", url="http://x/2")
    with db.connect() as conn:
        _, new1 = db.upsert_job(conn, a, 60, "r")
        _, new2 = db.upsert_job(conn, b, 60, "r")
        assert new1 is True and new2 is True
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


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
        main, _ = db.upsert_job(conn, J("1", title="ML Engineer A", url="http://x/1"), 60, "r")
        hidden, _ = db.upsert_job(conn, J("2", title="ML Engineer B", url="http://x/2"), 55, "r",
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
        keep, _ = db.upsert_job(conn, J("1", title="ML Engineer A", url="http://x/1"), 60, "r")
        drop, _ = db.upsert_job(conn, J("2", title="ML Engineer B", url="http://x/2"), 60, "r")
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
        idle, _ = db.upsert_job(conn, J("1", title="ML Engineer A", url="http://x/1"), 60, "r")   # new, untouched
        want, _ = db.upsert_job(conn, J("2", title="ML Engineer B", url="http://x/2"), 60, "r")
        db.set_feedback(conn, want, "interested")                          # engaged via label
        short, _ = db.upsert_job(conn, J("3", title="ML Engineer C", url="http://x/3"), 60, "r")
        db.update_status(conn, short, "shortlisted")                       # engaged via status

        assert {r["id"] for r in db.jobs_needing_enrichment(conn)} == {want, short}

        db.set_description(conn, want, "x" * 300)
        assert {r["id"] for r in db.jobs_needing_enrichment(conn)} == {short}   # want now full
        assert db.get_job(conn, want)["description_full"] == 1


def test_was_filtered_persists_and_false_negative_stats(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 55, "r",
                               filtered=True, filter_reason="senior title", seniority="senior")
        assert db.get_job(conn, jid)["was_filtered"] == 1
        db.set_feedback(conn, jid, "interested")               # rescue clears filtered
        row = db.get_job(conn, jid)
        assert row["filtered"] == 0 and row["was_filtered"] == 1   # history retained
        stats = db.false_negative_stats(conn)
        assert stats["interested"] == 1 and stats["false_negatives"] == 1
        assert stats["false_negative_rate"] == 1.0


def test_llm_and_cover_setters(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, jid, 88, "strong", "great fit")
        db.set_cover_letter(conn, jid, "/tmp/cl.md")
        rows = db.list_jobs(conn)
        assert rows[0]["llm_score"] == 88
        assert rows[0]["cover_letter_path"] == "/tmp/cl.md"
