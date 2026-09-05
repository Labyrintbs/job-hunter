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


def test_exclude_statuses_hides_but_pill_shows(tmp_db):
    with db.connect() as conn:
        keep, _ = db.upsert_job(conn, J("1", title="ML Engineer A", url="http://x/1"), 60, "r")
        dead, _ = db.upsert_job(conn, J("2", title="ML Engineer B", url="http://x/2"), 60, "r")
        db.update_status(conn, dead, "unavailable")
        assert [r["id"] for r in db.list_jobs(conn, exclude_statuses=("unavailable", "rejected"))] == [keep]
        assert [r["id"] for r in db.list_jobs(conn, status="unavailable")] == [dead]
        db.update_status(conn, dead, "rejected")
        assert [r["id"] for r in db.list_jobs(conn, exclude_statuses=("unavailable", "rejected"))] == [keep]
        assert [r["id"] for r in db.list_jobs(conn, status="rejected")] == [dead]


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


def test_role_category_stored_on_upsert(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", title="NLP Engineer", url="http://x/1"), 60, "r",
                               role_category="NLP")
        assert db.get_job(conn, jid)["role_category"] == "NLP"


def test_role_category_backfilled_for_preexisting_rows(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", title="Computer Vision Engineer", url="http://x/1"), 60, "r")
        assert db.get_job(conn, jid)["role_category"] == ""   # left unset by this upsert
    db.init_db()   # re-running the migration/backfill pass, as happens on every real startup
    with db.connect() as conn:
        assert db.get_job(conn, jid)["role_category"] == "CV"


def test_job_from_row_tolerates_null_text_columns(tmp_db):
    """A handful of old HelloWork rows have a genuine NULL description (stale
    data, not a live code path) -- job_from_row must coalesce nullable TEXT
    columns to "" rather than crash Job's pydantic validation."""
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        conn.execute(
            "UPDATE jobs SET description = NULL, location = NULL, url = NULL WHERE id = ?",
            (jid,),
        )
        row = db.get_job(conn, jid)
        job = db.job_from_row(row)
    assert job.description == "" and job.location == "" and job.url == ""


def test_llm_and_cover_setters(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, jid, 88, "strong", "great fit")
        db.set_cover_letter(conn, jid, "/tmp/cl.md")
        rows = db.list_jobs(conn)
        assert rows[0]["llm_score"] == 88
        assert rows[0]["cover_letter_path"] == "/tmp/cl.md"


def test_jobs_pending_enrichment_any_ignores_engagement_but_not_filtered_or_dismissed(tmp_db):
    with db.connect() as conn:
        plain, _ = db.upsert_job(conn, J("1", title="ML Engineer A", url="http://x/1"), 60, "r")
        hidden, _ = db.upsert_job(conn, J("2", title="ML Engineer B", url="http://x/2"), 55, "r",
                                  filtered=True, filter_reason="senior title")
        dismissed, _ = db.upsert_job(conn, J("3", title="ML Engineer C", url="http://x/3"), 60, "r")
        db.set_feedback(conn, dismissed, "dismissed")
        already_full, _ = db.upsert_job(conn, J("4", title="ML Engineer D", url="http://x/4"), 60, "r")
        db.set_description(conn, already_full, "x" * 300)

        pending = [r["id"] for r in db.jobs_pending_enrichment_any(conn, limit=10)]
    assert pending == [plain]   # not the filtered, dismissed, or already-full one


def test_jobs_ready_for_auto_tailor_needs_qualifying_verdict_and_no_cv(tmp_db):
    with db.connect() as conn:
        strong, _ = db.upsert_job(conn, J("1", title="A", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, strong, 90, "strong", "great fit")
        weak, _ = db.upsert_job(conn, J("2", title="B", url="http://x/2"), 60, "r")
        db.set_llm_judgment(conn, weak, 10, "weak", "poor fit")
        already_tailored, _ = db.upsert_job(conn, J("3", title="C", url="http://x/3"), 60, "r")
        db.set_llm_judgment(conn, already_tailored, 80, "good", "solid fit")
        db.add_cv_artifact(conn, already_tailored, "/tmp/cv.tex", "/tmp/cv.pdf", origin="ai")

        candidates = [r["id"] for r in db.jobs_ready_for_auto_tailor(conn, limit=10)]
    assert candidates == [strong]   # not the weak verdict, not the already-tailored one


def test_jobs_ready_for_auto_tailor_orders_by_score_and_respects_limit(tmp_db):
    with db.connect() as conn:
        low, _ = db.upsert_job(conn, J("1", title="A", url="http://x/1"), 60, "r")
        db.set_llm_judgment(conn, low, 55, "stretch", "uncertain fit")
        high, _ = db.upsert_job(conn, J("2", title="B", url="http://x/2"), 60, "r")
        db.set_llm_judgment(conn, high, 85, "good", "solid fit")

        candidates = [r["id"] for r in db.jobs_ready_for_auto_tailor(conn, limit=1)]
    assert candidates == [high]   # best fit first, capped at limit


def test_default_sort_prefers_llm_score_over_rule_score(tmp_db):
    """A high rule score with a low (informed) llm_score should rank below a lower
    rule score that the judge actually likes -- regression for the Decathlon case
    (rule=70, llm=15) outranking genuinely good fits."""
    with db.connect() as conn:
        high_rule_weak_llm, _ = db.upsert_job(
            conn, J("1", url="http://x/1", company="CoA"), 90, "r")
        db.set_llm_judgment(conn, high_rule_weak_llm, 15, "weak", "domain mismatch")
        low_rule_good_llm, _ = db.upsert_job(
            conn, J("2", url="http://x/2", company="CoB"), 40, "r")
        db.set_llm_judgment(conn, low_rule_good_llm, 85, "strong", "great fit")
        unjudged, _ = db.upsert_job(conn, J("3", url="http://x/3", company="CoC"), 60, "r")

        ids = [r["id"] for r in db.list_jobs(conn, filtered=None)]
        assert ids.index(low_rule_good_llm) < ids.index(unjudged) < ids.index(high_rule_weak_llm)


def test_set_llm_filter_hides_job_and_preserves_existing_reason(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_filter(conn, jid, "llm judge: weak fit")
        row = db.get_job(conn, jid)
        assert row["filtered"] == 1
        assert row["filter_reason"] == "llm judge: weak fit"

        # A second call (e.g. re-judged) appends rather than clobbering.
        db.set_llm_filter(conn, jid, "llm judge: weak fit (re-judged)")
        assert db.get_job(conn, jid)["filter_reason"] == \
            "llm judge: weak fit; llm judge: weak fit (re-judged)"
