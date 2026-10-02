from jobhunter import db, dupes
from jobhunter.models import Job

BODY = ("We are building a production LLM platform and need an engineer to design agentic pipelines, "
        "evaluate models, fine-tune them and ship them to customers with a small and friendly team. ") * 3
OTHER = ("The role focuses on classical computer vision for industrial inspection: calibration, "
         "segmentation of defects, embedded deployment and close work with the production line. ") * 3


def _job(ext, title="AI Engineer", company="Acme", loc="Paris", description=BODY):
    return Job(source="wttj", external_id=ext, title=title, company=company, location=loc,
               url=f"http://x/{ext}", description=description)


def test_identical_text_overlaps_fully_and_different_text_barely():
    a, b = dupes.shingles(BODY), dupes.shingles(OTHER)
    assert dupes.overlap(a, a) == 1.0
    assert dupes.overlap(a, b) < 0.1
    assert dupes.overlap(frozenset(), a) == 0.0 and dupes.shingles("too short") == frozenset()


def test_a_light_rewording_stays_between_the_candidate_and_identical_thresholds():
    reworded = BODY.replace("friendly", "supportive").replace("customers", "clients")
    ov = dupes.overlap(dupes.shingles(BODY), dupes.shingles(reworded))
    assert dupes.CANDIDATE_OVERLAP < ov < 1.0


def _row(i, status="new", geo="idf", fetched="2026-10-01 10:00:00"):
    return {"id": i, "status": status, "geo_tier": geo, "fetched_at": fetched}


def test_the_listing_you_acted_on_is_the_original_even_if_older():
    assert dupes.choose_original([_row(1, "applied", fetched="2026-09-01"), _row(2, "cv_ready")]) == 1


def test_the_furthest_along_listing_wins_among_several_engaged_ones():
    assert dupes.choose_original([_row(1, "rejected"), _row(2, "interview"), _row(3, "applied")]) == 2


def test_with_nothing_acted_on_a_cv_then_the_better_location_then_the_newest_wins():
    assert dupes.choose_original([_row(1, "new"), _row(2, "cv_ready", geo="outside")]) == 2
    assert dupes.choose_original([_row(1, geo="outside"), _row(2, geo="idf")]) == 2
    assert dupes.choose_original([_row(1, fetched="2026-09-01"), _row(2, fetched="2026-10-01")]) == 2


def test_same_company_in_two_cities_with_the_same_text_is_a_candidate(tmp_db):
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, _job("1", loc="Nantes"), 60, "r")
        b, _ = db.upsert_job(conn, _job("2", title="AI Engineer (H/F)", loc="Lyon"), 60, "r2")
        assert db.find_possible_duplicates(conn) == []                      # the cheap rules miss it
        pairs = db.find_possible_duplicates(conn, with_overlap=True)
    assert [{p["a"], p["b"]} for p in pairs] == [{a, b}] and pairs[0]["overlap"] == 1.0


def test_same_company_with_a_different_description_is_not_a_candidate(tmp_db):
    with db.connect() as conn:
        db.upsert_job(conn, _job("1", title="AI Engineer", loc="Paris"), 60, "r")
        db.upsert_job(conn, _job("2", title="Vision Engineer", loc="Paris", description=OTHER), 60, "r2")
        assert db.find_possible_duplicates(conn, with_overlap=True) == []


def test_a_short_description_is_never_compared_by_overlap(tmp_db):
    with db.connect() as conn:
        db.upsert_job(conn, _job("1", loc="Nantes", description="AI engineer wanted"), 60, "r")
        db.upsert_job(conn, _job("2", title="ML", loc="Lyon", description="AI engineer wanted"), 60, "r2")
        assert db.find_possible_duplicates(conn, with_overlap=True) == []


def test_the_dashboard_map_adds_pairs_the_checks_called_the_same(tmp_db):
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, _job("1", loc="Nantes"), 60, "r")
        b, _ = db.upsert_job(conn, _job("2", title="AI Engineer (H/F)", loc="Lyon"), 60, "r2")
        assert db.possible_duplicates_map(conn) == {}
        db.record_duplicate_check(conn, a, b, "same", "high", "identical")
        assert db.possible_duplicates_map(conn) == {a: [b], b: [a]}


def test_a_hidden_duplicate_you_never_acted_on_leaves_the_status_pill_and_count(tmp_db):
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, _job("1", loc="Nantes"), 60, "r")
        b, _ = db.upsert_job(conn, _job("2", title="AI Engineer (H/F)", loc="Lyon"), 60, "r2")
        for jid in (a, b):
            db.mark_cv_ready(conn, jid)
        db.set_llm_filter(conn, b, f"duplicate of #{a} (cv_ready) -- same posting")
        assert db.status_counts(conn) == {"cv_ready": 1}
        assert [r["id"] for r in db.list_jobs(conn, status="cv_ready")] == [a]
