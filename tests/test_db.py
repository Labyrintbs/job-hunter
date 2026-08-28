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


def test_llm_and_cover_setters(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, jid, 88, "strong", "great fit")
        db.set_cover_letter(conn, jid, "/tmp/cl.md")
        rows = db.list_jobs(conn)
        assert rows[0]["llm_score"] == 88
        assert rows[0]["cover_letter_path"] == "/tmp/cl.md"
