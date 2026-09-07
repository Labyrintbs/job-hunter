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


def test_cross_source_content_dedup_hellowork_department_code_location(tmp_db):
    # HelloWork has no comma in its location field at all -- it appends a trailing
    # department code instead ("Paris - 75"), which used to normalize to "paris 75"
    # and never match another source's bare "Paris" for the same city.
    wttj = Job(source="wttj", external_id="w1", title="Machine Learning Engineer",
               company="Doctolib", location="Paris", url="https://wttj.example/w1")
    hellowork = Job(source="hellowork", external_id="h1", title="Machine Learning Engineer",
                    company="Doctolib", location="Paris - 75", url="https://hellowork.example/h1")
    with db.connect() as conn:
        jid1, new1 = db.upsert_job(conn, wttj, 60, "r")
        jid2, new2 = db.upsert_job(conn, hellowork, 65, "r2")
        assert new1 is True and new2 is False
        assert jid1 == jid2


def test_cross_source_content_dedup_hellowork_arrondissement_location(tmp_db):
    # HelloWork sometimes folds the arrondissement into the city name itself
    # ("Paris 12e - 75", "Paris 1er - 75") rather than just appending a department
    # code -- this used to normalize to "paris 12e" and never match another
    # source's bare "Paris" for the same city.
    wttj = Job(source="wttj", external_id="w1", title="Senior AI Engineer",
               company="Converteo", location="Paris, Île-de-France, France",
               url="https://wttj.example/w1")
    hellowork = Job(source="hellowork", external_id="h1", title="Senior AI Engineer",
                    company="Converteo", location="Paris 12e - 75",
                    url="https://hellowork.example/h1")
    with db.connect() as conn:
        jid1, new1 = db.upsert_job(conn, wttj, 60, "r")
        jid2, new2 = db.upsert_job(conn, hellowork, 65, "r2")
        assert new1 is True and new2 is False
        assert jid1 == jid2


def test_find_possible_duplicates_flags_exact_title_at_same_company(tmp_db):
    # The same posting cross-listed at the exact same employer (e.g. HelloWork vs
    # LinkedIn), differing only by punctuation/boilerplate -- an EXACT match on the
    # de-junked title is required (not just a high ratio) so this stays safe: see
    # test_find_possible_duplicates_excludes_exact_same_company for the case (a
    # genuinely different role at the same company) this must NOT flag.
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, J("1", title="Senior AI Engineer H/F - CDI",
                                     company="Converteo", loc="Paris, Île-de-France, France"), 60, "r")
        b, _ = db.upsert_job(conn, J("2", title="Senior ai Engineer - CDI H/F",
                                     company="Converteo", loc="Paris 12e - 75"), 60, "r2")
        pairs = db.find_possible_duplicates(conn)
        assert len(pairs) == 1
        assert {pairs[0]["a"], pairs[0]["b"]} == {a, b}


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


def test_find_possible_duplicates_flags_related_company_and_similar_title(tmp_db):
    # Regression for the real Ubisoft case: HelloWork's "Ubisoft" vs LinkedIn's
    # "Ubisoft Paris Studio" for what's functionally the same posting -- different
    # enough (company string, title suffix) that upsert_job correctly keeps them as
    # two rows, but they should still surface as a "maybe check this" candidate.
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, J("1", title="Machine Learning Engineer H/F",
                                     company="Ubisoft", loc="Paris - 75"), 60, "r")
        b, _ = db.upsert_job(conn, J("2", title="Machine Learning Engineer - H/F/NB",
                                     company="Ubisoft Paris Studio", loc="Paris"), 60, "r2")
        pairs = db.find_possible_duplicates(conn)
        assert len(pairs) == 1
        assert {pairs[0]["a"], pairs[0]["b"]} == {a, b}
        dup_map = db.possible_duplicates_map(conn)
        assert dup_map[a] == [b]
        assert dup_map[b] == [a]


def test_find_possible_duplicates_ignores_generic_title_at_unrelated_company(tmp_db):
    # A common, generic title alone must never be treated as a duplicate signal --
    # two genuinely different employers frequently post an identically-titled role.
    with db.connect() as conn:
        db.upsert_job(conn, J("1", title="AI Engineer", company="Acme", loc="Paris"), 60, "r")
        db.upsert_job(conn, J("2", title="AI Engineer", company="Globex", loc="Paris"), 60, "r2")
        assert db.find_possible_duplicates(conn) == []


def test_find_possible_duplicates_excludes_exact_same_company(tmp_db):
    # Two similarly-worded titles at the EXACT same employer are far more often two
    # genuinely different open roles (different squad/level/specialization) than a
    # stray duplicate -- title-ratio alone can't reliably tell these apart (verified
    # against this project's real data), so the company axis must require a
    # related-but-different name, not just any match.
    with db.connect() as conn:
        db.upsert_job(conn, J("1", title="Senior Data Scientist I",
                              company="Rockerbox", loc="Paris"), 60, "r")
        db.upsert_job(conn, J("2", title="Sr Data Scientist II",
                              company="Rockerbox", loc="Paris"), 60, "r2")
        assert db.find_possible_duplicates(conn) == []


def test_find_possible_duplicates_requires_same_city(tmp_db):
    with db.connect() as conn:
        db.upsert_job(conn, J("1", title="Machine Learning Engineer H/F",
                              company="Ubisoft", loc="Paris"), 60, "r")
        db.upsert_job(conn, J("2", title="Machine Learning Engineer - H/F/NB",
                              company="Ubisoft Paris Studio", loc="Lyon"), 60, "r2")
        assert db.find_possible_duplicates(conn) == []


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


def _events(conn, job_id, event_type=None):
    rows = db.job_event_history(conn, job_id)
    if event_type:
        rows = [r for r in rows if r["event_type"] == event_type]
    return rows


def test_created_event_logged_on_new_job(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        rows = _events(conn, jid)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "created"
        assert rows[0]["from_value"] == "" and rows[0]["to_value"] == "new"


def test_no_created_event_on_reseen_job(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.upsert_job(conn, J("1", url="http://x/1"), 65, "r2")
        assert len(_events(conn, jid, "created")) == 1


def test_status_transition_logged_and_noop_skipped(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "shortlisted")
        rows = _events(conn, jid, "status")
        assert len(rows) == 1
        assert rows[0]["from_value"] == "new" and rows[0]["to_value"] == "shortlisted"

        db.update_status(conn, jid, "shortlisted")   # re-asserting the same status
        assert len(_events(conn, jid, "status")) == 1   # no new row


def test_filtered_transition_via_update_screening(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r", filtered=False)
        db.update_screening(conn, jid, 60, "r", filtered=True, filter_reason="too senior",
                            seniority="senior", min_years=5)
        db.update_screening(conn, jid, 60, "r", filtered=False, filter_reason="",
                            seniority="senior", min_years=5)
        rows = _events(conn, jid, "filtered")
        assert [r["from_value"] for r in rows] == ["0", "1"]
        assert [r["to_value"] for r in rows] == ["1", "0"]


def test_set_llm_filter_logs_event(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_filter(conn, jid, "weak fit")
        rows = _events(conn, jid, "filtered")
        assert len(rows) == 1
        assert rows[0]["from_value"] == "0" and rows[0]["to_value"] == "1"
        assert rows[0]["detail"] == "weak fit"


def test_set_filtered_restore_logs_event(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_filtered(conn, jid, True, "manual hide")
        db.set_filtered(conn, jid, False, "")
        rows = _events(conn, jid, "filtered")
        assert [r["from_value"] for r in rows] == ["0", "1"]
        assert [r["to_value"] for r in rows] == ["1", "0"]


def test_set_feedback_interested_logs_two_events_when_was_filtered(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_llm_filter(conn, jid, "weak fit")
        db.set_feedback(conn, jid, "interested", "actually a great fit")
        labels = _events(conn, jid, "label")
        filtereds = _events(conn, jid, "filtered")
        assert len(labels) == 1
        assert labels[0]["from_value"] == "" and labels[0]["to_value"] == "interested"
        # one filtered event from set_llm_filter, one from the interested rescue
        assert len(filtereds) == 2
        assert filtereds[-1]["from_value"] == "1" and filtereds[-1]["to_value"] == "0"


def test_set_feedback_interested_logs_only_label_when_not_filtered(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_feedback(conn, jid, "interested", "great fit")
        assert len(_events(conn, jid, "label")) == 1
        assert len(_events(conn, jid, "filtered")) == 0   # no-op skipped, job was never filtered


def test_set_feedback_dismissed_and_clear_chain_from_values(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.set_feedback(conn, jid, "dismissed", "wrong_domain")
        db.set_feedback(conn, jid, "", "")
        rows = _events(conn, jid, "label")
        assert [r["from_value"] for r in rows] == ["", "dismissed"]
        assert [r["to_value"] for r in rows] == ["dismissed", ""]


def test_job_event_history_ordered(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "shortlisted")
        db.update_status(conn, jid, "cv_ready")
        rows = db.job_event_history(conn, jid)
        assert [r["event_type"] for r in rows] == ["created", "status", "status"]
        assert [r["to_value"] for r in rows] == ["new", "shortlisted", "cv_ready"]


def test_all_job_events_filters_by_type_and_since(tmp_db):
    with db.connect() as conn:
        jid1, _ = db.upsert_job(conn, J("1", url="http://x/1", company="CoA"), 60, "r")
        jid2, _ = db.upsert_job(conn, J("2", url="http://x/2", company="CoB"), 60, "r")
        db.update_status(conn, jid1, "shortlisted")
        status_events = db.all_job_events(conn, event_type="status")
        assert len(status_events) == 1
        assert status_events[0]["job_id"] == jid1
        assert status_events[0]["company"] == "CoA"

        all_events = db.all_job_events(conn)
        assert len(all_events) == 3   # 2 created + 1 status


def test_status_transition_counts_aggregates(tmp_db):
    with db.connect() as conn:
        jid1, _ = db.upsert_job(conn, J("1", url="http://x/1", company="CoA"), 60, "r")
        jid2, _ = db.upsert_job(conn, J("2", url="http://x/2", company="CoB"), 60, "r")
        db.update_status(conn, jid1, "shortlisted")
        db.update_status(conn, jid2, "shortlisted")
        db.update_status(conn, jid1, "cv_ready")
        counts = {(r["from_value"], r["to_value"]): r["n"] for r in db.status_transition_counts(conn)}
        assert counts[("new", "shortlisted")] == 2
        assert counts[("shortlisted", "cv_ready")] == 1


def test_stage_reach_counts(tmp_db):
    with db.connect() as conn:
        jid1, _ = db.upsert_job(conn, J("1", url="http://x/1", company="CoA"), 60, "r")
        jid2, _ = db.upsert_job(conn, J("2", url="http://x/2", company="CoB"), 60, "r")
        db.update_status(conn, jid1, "shortlisted")
        db.update_status(conn, jid1, "cv_ready")
        db.update_status(conn, jid1, "applied")
        db.update_status(conn, jid2, "shortlisted")
        counts = {r["stage"]: r["n"] for r in db.stage_reach_counts(conn, ("shortlisted", "cv_ready", "applied"))}
        assert counts == {"shortlisted": 2, "cv_ready": 1, "applied": 1}


def test_backfill_creates_events_for_preexisting_rows(tmp_db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs (source, external_id, title, company, fetched_at, "
            "filtered, user_label, labeled_at) VALUES "
            "('wttj', 'preexist-1', 'ML Engineer', 'Acme', '2026-01-01 00:00:00', "
            "1, 'interested', '2026-01-02 00:00:00')"
        )
        jid = conn.execute("SELECT id FROM jobs WHERE external_id = 'preexist-1'").fetchone()[0]
        conn.execute(
            "INSERT INTO applications (job_id, status, updated_at) VALUES (?, 'applied', '2026-01-03 00:00:00')",
            (jid,))
    # simulate a pre-feature row: no job_events yet for this job_id
    with db.connect() as conn:
        assert len(_events(conn, jid)) == 0

    db.init_db(db_path=db.DB_PATH)

    with db.connect() as conn:
        rows = _events(conn, jid)
        types = sorted(r["event_type"] for r in rows)
        assert types == ["created", "filtered", "label", "status"]
        for r in rows:
            assert r["source"] == "backfill"


def test_backfill_is_idempotent(tmp_db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs (source, external_id, title, company, fetched_at) "
            "VALUES ('wttj', 'preexist-2', 'ML Engineer', 'Acme', '2026-01-01 00:00:00')")
        jid = conn.execute("SELECT id FROM jobs WHERE external_id = 'preexist-2'").fetchone()[0]
        conn.execute("INSERT INTO applications (job_id, status) VALUES (?, 'new')", (jid,))

    db.init_db(db_path=db.DB_PATH)
    with db.connect() as conn:
        count1 = len(_events(conn, jid))
    db.init_db(db_path=db.DB_PATH)
    with db.connect() as conn:
        count2 = len(_events(conn, jid))
    assert count1 == count2 == 1   # just the 'created' event, status stayed 'new'


def test_backfill_skips_jobs_with_live_events(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
    db.init_db(db_path=db.DB_PATH)   # re-run backfill after a normal, live-logged insert
    with db.connect() as conn:
        assert len(_events(conn, jid)) == 1   # still just the one live 'created' event


def test_search_matches_company_title_or_location_case_insensitive(tmp_db):
    with db.connect() as conn:
        by_company, _ = db.upsert_job(
            conn, J("1", title="ML Engineer", company="Ubisoft", loc="Paris", url="http://x/1"), 60, "r")
        by_title, _ = db.upsert_job(
            conn, J("2", title="Senior Data Scientist", company="Acme", loc="Paris", url="http://x/2"), 60, "r")
        by_location, _ = db.upsert_job(
            conn, J("3", title="ML Engineer", company="Globex", loc="Lyon", url="http://x/3"), 60, "r")
        db.upsert_job(
            conn, J("4", title="Backend Engineer", company="Other Co", loc="Nantes", url="http://x/4"), 60, "r")

        assert [r["id"] for r in db.list_jobs(conn, q="ubisoft")] == [by_company]
        assert [r["id"] for r in db.list_jobs(conn, q="DATA SCIENTIST")] == [by_title]
        assert [r["id"] for r in db.list_jobs(conn, q="lyon")] == [by_location]
        assert db.list_jobs(conn, q="nonexistentxyz") == []


def test_search_bypasses_filtered_dismissed_and_score_when_buckets_neutralized(tmp_db):
    # Mirrors what the dashboard route does when a search query is active: a job
    # can be filtered, dismissed, and below the usual score cutoff, and still be
    # found by name once filtered/dismissed/interested are passed as None.
    with db.connect() as conn:
        hidden, _ = db.upsert_job(
            conn, J("1", title="ML Engineer", company="Shift Technology", loc="Paris", url="http://x/1"),
            10, "r", filtered=True, filter_reason="senior title")
        db.set_feedback(conn, hidden, "dismissed", "too_senior")

        assert db.list_jobs(conn) == []   # default main-list view hides it (filtered)

        results = db.list_jobs(conn, min_score=0, filtered=None, dismissed=None,
                               interested=None, q="shift")
        assert [r["id"] for r in results] == [hidden]
