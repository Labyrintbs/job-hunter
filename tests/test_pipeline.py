import json
import subprocess
from pathlib import Path

import pytest

from jobhunter import db, fetch_diag, pipeline
from jobhunter.llm import provider
from jobhunter.models import Job
from jobhunter.tailor import engine as cv_engine


def test_gather_survives_a_source_exception(tmp_db, config, monkeypatch):
    """A WTTJ (or any source) failure must not crash the whole run -- previously
    only ats/linkedin were wrapped in try/except; wttj wasn't."""
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies, workday_queries=None: [])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_arbeitnow", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_eures", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_aijobs", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_free_work", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_lesjeudis", lambda cfg: [])

    jobs = pipeline._gather(config)   # must not raise
    assert jobs == []


def test_gather_collects_every_source(tmp_db, config, monkeypatch):
    make = lambda src, i: Job(source=src, external_id=str(i), title="ML Engineer", company="Acme")
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: [make("wttj", 1)])
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies, workday_queries=None: [make("ats", 2)])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [make("linkedin", 3)])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [make("francetravail", 4)])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: [make("hellowork", 5)])
    monkeypatch.setattr(pipeline, "_fetch_arbeitnow", lambda cfg: [make("arbeitnow", 6)])
    monkeypatch.setattr(pipeline, "_fetch_eures", lambda cfg: [make("eures", 7)])
    monkeypatch.setattr(pipeline, "_fetch_aijobs", lambda cfg: [make("aijobs", 8)])
    monkeypatch.setattr(pipeline, "_fetch_free_work", lambda cfg: [make("free_work", 9)])
    monkeypatch.setattr(pipeline, "_fetch_lesjeudis", lambda cfg: [make("lesjeudis", 10)])

    jobs = pipeline._gather(config)
    assert {j.source for j in jobs} == {"wttj", "ats", "linkedin", "francetravail",
                                         "hellowork", "arbeitnow", "eures", "aijobs",
                                         "free_work", "lesjeudis"}


def test_gather_times_each_fetched_source_but_not_skipped_ones(tmp_db, config, monkeypatch):
    called = []
    _stub_all_sources_except_hellowork(monkeypatch, called)
    with db.connect() as conn:
        db.record_source_fetch(conn, "hellowork", 5)   # just fetched -> not due again
    pipeline._gather(config)
    assert "hellowork" not in pipeline._gather_seconds
    assert "linkedin" in pipeline._gather_seconds
    assert all(s >= 0 for s in pipeline._gather_seconds.values())


def test_run_fetch_stores_timings_with_the_run(tmp_db, config, monkeypatch):
    import json
    monkeypatch.setattr(pipeline, "_gather",
                        lambda cfg, force=False: pipeline._gather_seconds.update(wttj=1.5) or [])
    stats = pipeline.run_fetch(config)
    assert stats["timings"]["persist"] >= 0 and stats["timings"]["total"] >= stats["timings"]["persist"]
    with db.connect() as conn:
        stored = json.loads(conn.execute("SELECT timings FROM fetch_runs").fetchone()["timings"])
    assert stored["sources"] == {"wttj": 1.5}
    assert set(stored) == {"sources", "persist", "total"}


def _stub_all_sources_except_hellowork(monkeypatch, called):
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: [])
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies, workday_queries=None: [])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: called.append(1) or [])
    monkeypatch.setattr(pipeline, "_fetch_arbeitnow", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_eures", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_aijobs", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_free_work", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_lesjeudis", lambda cfg: [])


def test_gather_skips_a_source_whose_interval_has_not_elapsed(tmp_db, config, monkeypatch):
    called: list = []
    _stub_all_sources_except_hellowork(monkeypatch, called)
    with db.connect() as conn:
        db.record_source_fetch(conn, "hellowork", 3)   # just fetched -- hellowork's interval is 24h

    pipeline._gather(config)

    assert called == []   # not due yet, skipped


def test_gather_fetches_a_source_once_its_interval_has_elapsed(tmp_db, config, monkeypatch):
    called: list = []
    _stub_all_sources_except_hellowork(monkeypatch, called)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO source_fetch_state (source, last_attempted_at, last_count) VALUES (?,?,?)",
            ("hellowork", "2000-01-01 00:00:00", 0),
        )

    pipeline._gather(config)

    assert called == [1]
    with db.connect() as conn:
        assert db.get_source_fetch_state(conn, "hellowork")["last_count"] == 0


def test_gather_force_bypasses_interval_gating(tmp_db, config, monkeypatch):
    called: list = []
    _stub_all_sources_except_hellowork(monkeypatch, called)
    with db.connect() as conn:
        db.record_source_fetch(conn, "hellowork", 3)   # just fetched

    pipeline._gather(config, force=True)

    assert called == [1]   # force bypasses the not-due gate


def test_fetch_wttj_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_hits, country):
        calls.append(query)
        return [Job(source="wttj", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.wttj, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer", "max_hits": 100, "countries": ["France"],
           "wttj": {"queries": ["machine learning engineer", "nlp engineer"],
                     "fetch_europe_remote": False}}
    jobs = pipeline._fetch_wttj(cfg)
    assert calls == ["machine learning engineer", "nlp engineer"]
    assert len(jobs) == 2


def test_fetch_wttj_also_fetches_europe_remote_pass_by_default(monkeypatch):
    calls = []

    def fake_fetch(query, max_hits, country=None, remote_only=False, extra_countries=None):
        calls.append((query, remote_only))
        return [Job(source="wttj", external_id=f"{query}-{remote_only}", title=query, company="Acme")]

    monkeypatch.setattr(pipeline.wttj, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer", "max_hits": 100, "countries": ["France"],
           "europe_countries": ["Germany", "Spain"],
           "wttj": {"queries": ["machine learning engineer", "nlp engineer"]}}
    jobs = pipeline._fetch_wttj(cfg)
    # one France-scoped call and one remote_only call per query
    assert calls == [
        ("machine learning engineer", False), ("nlp engineer", False),
        ("machine learning engineer", True), ("nlp engineer", True),
    ]
    assert len(jobs) == 4


def test_fetch_linkedin_also_fetches_europe_remote_pass_by_default(monkeypatch):
    """f_WT=2 here is only a coarse pre-filter (verified unreliable as a remote
    signal on its own) -- results keep their real location; confirming genuine
    remote status happens later from full JD text (see enrich_one)."""
    calls = []

    def fake_fetch(queries, locations, max_pages=5, recent_hours=168, max_retries=3,
                    backoff_base=2.0, workplace_type=None):
        calls.append((tuple(locations), workplace_type))
        return [Job(source="linkedin", external_id=f"{locations}-{workplace_type}",
                     title="ML Engineer", company="Acme")]

    monkeypatch.setattr(pipeline.linkedin, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "linkedin": {"enabled": True, "queries": ["machine learning engineer"],
                         "locations": ["France"]}}
    jobs = pipeline._fetch_linkedin(cfg)
    assert calls == [(("France",), None), (("Europe",), "2")]
    assert len(jobs) == 2


def test_fetch_linkedin_europe_remote_pass_can_be_disabled(monkeypatch):
    calls = []

    def fake_fetch(queries, locations, max_pages=5, recent_hours=168, max_retries=3,
                    backoff_base=2.0, workplace_type=None):
        calls.append((tuple(locations), workplace_type))
        return []

    monkeypatch.setattr(pipeline.linkedin, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "linkedin": {"enabled": True, "queries": ["machine learning engineer"],
                         "locations": ["France"], "europe_remote": {"enabled": False}}}
    pipeline._fetch_linkedin(cfg)
    assert calls == [(("France",), None)]


def test_fetch_francetravail_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, departements):
        calls.append(query)
        return [Job(source="francetravail", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.francetravail, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "francetravail": {"enabled": True, "departements": "75",
                             "queries": ["machine learning engineer", "computer vision engineer"]}}
    jobs = pipeline._fetch_francetravail(cfg)
    assert calls == ["machine learning engineer", "computer vision engineer"]
    assert len(jobs) == 2


def test_fetch_arbeitnow_passes_configured_max_pages(monkeypatch):
    calls = []

    def fake_fetch(max_pages=5):
        calls.append(max_pages)
        return [Job(source="arbeitnow", external_id="1", title="ML Engineer", company="Acme")]

    monkeypatch.setattr(pipeline.arbeitnow, "fetch", fake_fetch)
    cfg = {"arbeitnow": {"enabled": True, "max_pages": 7}}
    jobs = pipeline._fetch_arbeitnow(cfg)
    assert calls == [7]
    assert len(jobs) == 1


def test_fetch_arbeitnow_disabled_returns_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.arbeitnow, "fetch", lambda **k: called.append(1) or [])
    cfg = {"arbeitnow": {"enabled": False}}
    jobs = pipeline._fetch_arbeitnow(cfg)
    assert jobs == []
    assert called == []


def test_fetch_eures_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_hits):
        calls.append((query, max_hits))
        return [Job(source="eures", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.eures, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "eures": {"enabled": True, "max_hits": 50,
                     "queries": ["machine learning engineer", "nlp engineer"]}}
    jobs = pipeline._fetch_eures(cfg)
    assert calls == [("machine learning engineer", 50), ("nlp engineer", 50)]
    assert len(jobs) == 2


def test_fetch_eures_disabled_returns_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.eures, "fetch", lambda **k: called.append(1) or [])
    cfg = {"eures": {"enabled": False}}
    assert pipeline._fetch_eures(cfg) == []
    assert called == []


def test_fetch_aijobs_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_hits):
        calls.append((query, max_hits))
        return [Job(source="aijobs", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.aijobs, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "aijobs": {"enabled": True, "max_hits": 30, "queries": ["ai engineer"]}}
    jobs = pipeline._fetch_aijobs(cfg)
    assert calls == [("ai engineer", 30)]
    assert len(jobs) == 1


def test_fetch_aijobs_disabled_returns_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.aijobs, "fetch", lambda **k: called.append(1) or [])
    cfg = {"aijobs": {"enabled": False}}
    assert pipeline._fetch_aijobs(cfg) == []
    assert called == []


def test_fetch_free_work_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_detail_fetches):
        calls.append((query, max_detail_fetches))
        return [Job(source="free_work", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.free_work, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "free_work": {"enabled": True, "max_detail_fetches": 20, "queries": ["data scientist"]}}
    jobs = pipeline._fetch_free_work(cfg)
    assert calls == [("data scientist", 20)]
    assert len(jobs) == 1


def test_fetch_free_work_disabled_returns_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.free_work, "fetch", lambda **k: called.append(1) or [])
    cfg = {"free_work": {"enabled": False}}
    assert pipeline._fetch_free_work(cfg) == []
    assert called == []


def test_fetch_lesjeudis_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_detail_fetches):
        calls.append((query, max_detail_fetches))
        return [Job(source="lesjeudis", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.lesjeudis, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer",
           "lesjeudis": {"enabled": True, "max_detail_fetches": 20, "queries": ["data scientist"]}}
    jobs = pipeline._fetch_lesjeudis(cfg)
    assert calls == [("data scientist", 20)]
    assert len(jobs) == 1


def test_fetch_lesjeudis_disabled_returns_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline.lesjeudis, "fetch", lambda **k: called.append(1) or [])
    cfg = {"lesjeudis": {"enabled": False}}
    assert pipeline._fetch_lesjeudis(cfg) == []
    assert called == []


def _insert(conn, config, **kw):
    job = Job(source=kw.pop("source", "linkedin"), external_id=kw.pop("external_id", "1"),
              title=kw.pop("title", "Machine Learning Engineer"), company=kw.pop("company", "Acme"),
              location=kw.pop("location", "Paris, Ile-de-France, France"),
              description=kw.pop("description", ""), **kw)
    from jobhunter import match
    s = match.screen(job, config)
    jid, _ = db.upsert_job(conn, job, s.score, s.reasons, filtered=s.filtered,
                           filter_reason=s.filter_reason, seniority=s.seniority,
                           min_years=s.min_years, role_category=s.role_category)
    return jid


def test_enrich_one_rescopes_with_real_content(tmp_db, config, monkeypatch, tmp_path):
    with db.connect() as conn:
        jid = _insert(conn, config)   # title-only: no boost keywords yet
        before = db.get_job(conn, jid)

    rich_text = "We build with pytorch, deep learning and mlops pipelines for computer vision."
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: rich_text)

    result = pipeline.enrich_one(jid)
    assert result["enriched"] is True
    assert result["score"] > before["score"]   # real content adds boost-keyword points

    with db.connect() as conn:
        after = db.get_job(conn, jid)
    assert after["description"] == rich_text
    assert after["description_full"] == 1
    assert after["score"] == result["score"]

    jd_file = tmp_path / "jd" / f"linkedin__{after['external_id']}.txt"
    assert jd_file.exists()
    assert rich_text in jd_file.read_text(encoding="utf-8")


def test_enrich_one_confirms_remote_from_full_jd_text(tmp_db, config, monkeypatch):
    """A job whose location doesn't say remote (e.g. a LinkedIn card from the
    europe_remote pre-filter pass) gets tagged and re-tiered once the real JD
    text confirms it -- see match.detect_remote_from_text."""
    with db.connect() as conn:
        jid = _insert(conn, config, location="Berlin, Germany")

    remote_jd = "We are a fully remote team. " + _REAL_JD
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: remote_jd)

    pipeline.enrich_one(jid)

    with db.connect() as conn:
        after = db.get_job(conn, jid)
    assert after["location"] == "Berlin, Germany - Remote"
    assert after["geo_tier"] == "europe_remote"


def test_enrich_one_leaves_location_alone_when_jd_does_not_confirm_remote(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, location="Berlin, Germany")

    onsite_jd = "You will join our Berlin office team on-site. " + _REAL_JD
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: onsite_jd)

    pipeline.enrich_one(jid)

    with db.connect() as conn:
        after = db.get_job(conn, jid)
    assert after["location"] == "Berlin, Germany"
    assert after["geo_tier"] == "outside"


def test_enrich_one_does_not_double_tag_already_remote_location(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, location="Berlin, Germany - Remote")

    remote_jd = "This role is fully remote. " + _REAL_JD
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: remote_jd)

    pipeline.enrich_one(jid)

    with db.connect() as conn:
        after = db.get_job(conn, jid)
    assert after["location"].count("Remote") == 1


def test_run_fetch_persists_role_category(tmp_db, config, monkeypatch):
    cv_job = Job(source="wttj", external_id="42", title="Computer Vision Engineer",
                company="Acme", location="Paris, Ile-de-France, France")
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: [cv_job])
    stats = pipeline.run_fetch(config)
    with db.connect() as conn:
        row = db.get_job(conn, stats["new_ids"][0])
    assert row["role_category"] == "CV"


def test_run_fetch_precreates_cv_folder_for_kept_not_filtered_jobs(tmp_db, config, monkeypatch):
    kept = Job(source="wttj", external_id="1", title="Computer Vision Engineer",
               company="Acme", location="Paris, Ile-de-France, France")
    filtered = Job(source="wttj", external_id="2", title="ML Engineer",
                    company="OtherCo", location="Paris, Ile-de-France, France",
                    description="French citizenship is required for this role.")
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: [kept, filtered])
    stats = pipeline.run_fetch(config)

    with db.connect() as conn:
        kept_row = db.get_job(conn, stats["new_ids"][0])
        filtered_id = [r["id"] for r in db.list_jobs(conn, filtered=1)][0]
        filtered_row = db.get_job(conn, filtered_id)
    assert filtered_row["filtered"] == 1

    kept_dir = cv_engine.CV_OUT_DIR / f"{kept_row['id']}-{cv_engine._slug(kept_row['company'])}"
    filtered_dir = cv_engine.CV_OUT_DIR / f"{filtered_row['id']}-{cv_engine._slug(filtered_row['company'])}"
    assert kept_dir.is_dir()
    assert not filtered_dir.exists()


def test_import_manual_job_screens_and_upserts_like_a_normal_fetch(tmp_db, config):
    result = pipeline.import_manual_job(
        title="Machine Learning Engineer", company="BigCorp",
        url="https://bigcorp.example/careers/123", location="Paris, France")
    assert result["kept"] is True
    assert result["is_new"] is True
    with db.connect() as conn:
        row = db.get_job(conn, result["job_id"])
    assert row["source"] == "manual"
    assert row["company"] == "BigCorp"
    assert row["role_category"] == "ML/DL"
    cv_dir = cv_engine.CV_OUT_DIR / f"{row['id']}-{cv_engine._slug('BigCorp')}"
    assert cv_dir.is_dir()


def test_import_manual_job_not_relevant_is_not_stored(tmp_db, config):
    result = pipeline.import_manual_job(
        title="Accountant", company="BigCorp", url="https://bigcorp.example/careers/456")
    assert result["kept"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_import_manual_job_merges_with_existing_cross_source_posting(tmp_db, config):
    with db.connect() as conn:
        existing_id = _insert(conn, config, source="linkedin", title="Machine Learning Engineer",
                              company="BigCorp", location="Paris")
    result = pipeline.import_manual_job(
        title="Machine Learning Engineer", company="BigCorp",
        url="https://bigcorp.example/careers/789", location="Paris, Île-de-France, France")
    assert result["is_new"] is False
    assert result["job_id"] == existing_id


def test_enrich_one_drops_jd_copy_into_the_cv_folder(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, source="linkedin", external_id="7", company="Acme")

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text",
                        lambda *a, **k: "We build with pytorch and deep learning models.")
    pipeline.enrich_one(jid)

    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["filtered"] == 0
    jd_copy = cv_engine.CV_OUT_DIR / f"{jid}-{cv_engine._slug(row['company'])}" / "jd.txt"
    assert jd_copy.exists()
    assert "pytorch" in jd_copy.read_text(encoding="utf-8")


def test_enrich_one_skips_cv_folder_copy_when_enrichment_flips_to_filtered(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, source="linkedin", external_id="8", company="OtherCo")

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text",
                        lambda *a, **k: "French citizenship is required for this role.")
    result = pipeline.enrich_one(jid)
    assert result["filtered"] is True

    jd_copy = cv_engine.CV_OUT_DIR / f"{jid}-{cv_engine._slug('OtherCo')}" / "jd.txt"
    assert not jd_copy.exists()


def test_enrich_one_can_flip_role_category(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, title="Machine Learning Engineer")   # generic -> ML/DL
        before = db.get_job(conn, jid)
    assert before["role_category"] == "ML/DL"

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text",
                        lambda *a, **k: "We focus on named entity recognition and sentiment analysis.")
    pipeline.enrich_one(jid)

    with db.connect() as conn:
        after = db.get_job(conn, jid)
    assert after["role_category"] == "NLP"   # re-screened with the enriched body


def test_enrich_new_skips_jobs_that_already_have_a_description(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        has_desc = _insert(conn, config, source="wttj", external_id="1", company="Acme",
                           description="x" * 300)   # already full at fetch time
        no_desc = _insert(conn, config, source="linkedin", external_id="2", company="OtherCo",
                          description="")

    calls = []

    def fake_fetch(source, ext, url, **k):
        calls.append((source, ext))
        return "fresh description text with pytorch mlops"

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", fake_fetch)
    result = pipeline.enrich_new([has_desc, no_desc])

    assert result["candidates"] == 1 and result["enriched"] == 1
    assert calls == [("linkedin", "2")]   # only the job lacking a real description was fetched


def test_daily_run_enriches_before_judging(tmp_db, config, monkeypatch):
    # One fresh LinkedIn job (no description at fetch time, like real guest cards).
    fresh_job = Job(source="linkedin", external_id="99", title="Machine Learning Engineer",
                    company="Acme", location="Paris, Ile-de-France, France", url="http://x/99")
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: [fresh_job])

    marker = "SPECIAL_MARKER_ONLY_PRESENT_AFTER_ENRICHMENT " * 3   # clears the judge's min-length gate
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: marker)
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)

    captured = {}

    def fake_judge(job, preferences=""):
        captured["description"] = job.description
        return {"score": 80, "verdict": "good", "seniority": "junior",
                "min_years": 1, "reasons": "fit"}

    monkeypatch.setattr(pipeline.llm_judge, "judge", fake_judge)
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: None)

    summary = pipeline.daily_run(judge=True)

    assert summary["judged"] == 1
    assert captured["description"] == marker   # judge saw the enriched content, not empty/title-only


_REAL_JD = "x" * 150   # clears judge_one's _MIN_DESCRIPTION_CHARS gate


def test_judge_one_auto_hides_weak_verdict(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 15, "verdict": "weak", "seniority": "junior",
                         "min_years": 0, "reasons": "domain mismatch"})

    pipeline.judge_one(jid)

    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["llm_score"] == 15
    assert row["filtered"] == 1
    assert row["filter_reason"] == "llm judge: weak fit"


def test_judge_one_skips_jobs_with_no_real_description(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description="")
    called = []
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="": called.append(1))

    result = pipeline.judge_one(jid)

    assert result.get("skipped")
    assert called == []   # never even called the LLM -- no ungrounded guess
    with db.connect() as conn:
        assert db.get_job(conn, jid)["llm_score"] is None


def test_judge_all_excludes_permanently_unfetchable_jobs_from_the_queue(tmp_db, config, monkeypatch):
    """A job that exhausted every enrichment retry with no real JD text must not
    occupy a limit slot forever, ahead of a genuinely judgeable job behind it."""
    with db.connect() as conn:
        # keyword-rich teaser text (not a real JD) -> outscores the plain judgeable
        # job below, so without the fix it would sort to the front of the queue.
        # Distinct companies: same title+company+location would silently merge
        # via upsert_job's cross-source dedup instead of creating two rows.
        stuck = _insert(conn, config, external_id="stuck", company="StuckCo",
                        description="machine learning deep learning nlp mlops pytorch")
        for _ in range(db.MAX_ENRICH_ATTEMPTS):
            db.bump_enrich_attempts(conn, stuck)
        judgeable = _insert(conn, config, external_id="ok", company="OkCo", description=_REAL_JD)

    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 70, "verdict": "good", "seniority": "junior",
                         "min_years": 0, "reasons": "solid fit"})

    stats = pipeline.judge_all(min_score=0, limit=1)   # limit=1: only room for one candidate

    assert stats["judged"] == 1
    with db.connect() as conn:
        assert db.get_job(conn, judgeable)["llm_score"] == 70   # the real candidate got the slot
        assert db.get_job(conn, stuck)["llm_score"] is None     # never even attempted


_LONG_REAL_JD = "x" * 250   # >200 chars so description_full=1 at insert -- skips enrich_new's throttled fetch


def _make_judgeable_jobs(n):
    return [Job(source="wttj", external_id=str(i), title="Machine Learning Engineer",
                company=f"Co{i}", location="Paris, Ile-de-France, France",
                url=f"http://x/{i}", description=_LONG_REAL_JD)
            for i in range(1, n + 1)]


def test_daily_run_auto_tailors_everything_but_weak_verdicts(tmp_db, config, monkeypatch):
    jobs = _make_judgeable_jobs(4)
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: jobs)
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: None)

    verdicts = {"1": "strong", "2": "weak", "3": "good", "4": "stretch"}

    def fake_judge(job, preferences=""):
        v = verdicts[job.external_id]
        return {"score": 10 if v == "weak" else 80, "verdict": v,
                "seniority": "junior", "min_years": 0, "reasons": "r"}

    monkeypatch.setattr(pipeline.llm_judge, "judge", fake_judge)
    tailored_ids, covered_ids = [], []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False:
                        tailored_ids.append(jid) or {"job_id": jid, "compiled": True})
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: covered_ids.append(jid) or {"job_id": jid})

    summary = pipeline.daily_run(judge=True)

    assert summary["tailored"] == 3
    assert len(tailored_ids) == 3 and len(covered_ids) == 3   # not the "weak" job


def test_daily_run_respects_auto_tailor_limit(tmp_db, config, monkeypatch):
    jobs = _make_judgeable_jobs(3)
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: jobs)
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: None)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 80, "verdict": "strong", "seniority": "junior",
                         "min_years": 0, "reasons": "r"})
    tailored_ids = []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False:
                        tailored_ids.append(jid) or {"job_id": jid, "compiled": True})
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: {"job_id": jid})

    summary = pipeline.daily_run(judge=True, auto_tailor_limit=2)

    assert summary["tailored"] == 2
    assert len(tailored_ids) == 2   # capped even though all 3 qualified


def test_daily_run_auto_tailor_false_skips_entirely(tmp_db, config, monkeypatch):
    jobs = _make_judgeable_jobs(1)
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: jobs)
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: None)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 80, "verdict": "strong", "seniority": "junior",
                         "min_years": 0, "reasons": "r"})
    called = []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False: called.append(jid))
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: called.append(jid))

    summary = pipeline.daily_run(judge=True, auto_tailor=False)

    assert summary["tailored"] == 0
    assert called == []


def test_judge_one_does_not_hide_good_or_stretch_verdicts(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 55, "verdict": "stretch", "seniority": "junior",
                         "min_years": 0, "reasons": "uncertain fit"})

    pipeline.judge_one(jid)

    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["llm_score"] == 55
    assert row["filtered"] == 0


def _fake_verdict(verdict, score=80):
    return lambda job, preferences="": {"score": score, "verdict": verdict,
                                         "seniority": "junior", "min_years": 0, "reasons": "r"}


def test_judge_one_probes_ats_for_new_company_on_good_verdict(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, company="Brand New Startup", description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("good"))
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])   # not in companies.yaml
    probed, added = [], []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw:
                        probed.append(company) or pipeline.ats_discovery.Hit("greenhouse", "x", 3))
    monkeypatch.setattr(pipeline, "add_company", lambda *a: added.append(a))

    pipeline.judge_one(jid)

    assert probed == ["Brand New Startup"]
    assert added == [("Brand New Startup", "greenhouse", "x")]   # promoted without manual confirmation
    with db.connect() as conn:
        companies = {c["name"]: c["last_result"] for c in db.list_target_companies(conn)}
    assert companies["Brand New Startup"] == \
        "auto-added to companies.yaml: greenhouse board, token=x, 3 postings"


def test_judge_one_does_not_probe_ats_on_weak_or_stretch_verdict(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid_weak = _insert(conn, config, company="Weak Co", external_id="w1", description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("weak", score=10))
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    probed = []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: probed.append(company))
    pipeline.judge_one(jid_weak)

    with db.connect() as conn:
        jid_stretch = _insert(conn, config, company="Stretch Co", external_id="s1", description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("stretch", score=50))
    pipeline.judge_one(jid_stretch)

    assert probed == []
    with db.connect() as conn:
        assert db.list_target_companies(conn) == []


def test_judge_one_skips_probe_for_company_already_in_companies_yaml(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, company="Already Automated Co", description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("strong"))
    monkeypatch.setattr(pipeline, "load_companies", lambda: [{"name": "Already Automated Co", "ats": "lever"}])
    probed = []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: probed.append(company))

    pipeline.judge_one(jid)

    assert probed == []
    with db.connect() as conn:
        assert db.list_target_companies(conn) == []


def test_judge_one_only_probes_a_company_once(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("good"))
    probed = []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: probed.append(company) or None)

    with db.connect() as conn:
        jid1 = _insert(conn, config, company="Repeat Co", external_id="r1", description=_REAL_JD)
    pipeline.judge_one(jid1)

    with db.connect() as conn:
        jid2 = _insert(conn, config, company="Repeat Co", external_id="r2", description=_REAL_JD)
    pipeline.judge_one(jid2)

    assert probed == ["Repeat Co"]   # second good verdict for the same company: no re-probe


def test_failed_probe_is_tracked_and_retried_on_the_next_good_verdict(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("good"))
    outcomes = [pipeline.ats_discovery.ProbeIncomplete("3 probe request(s) failed transiently"), None]
    probed = []

    def flaky_probe(company, **kw):
        probed.append(company)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pipeline.ats_discovery, "probe", flaky_probe)

    with db.connect() as conn:
        jid1 = _insert(conn, config, company="Flaky Co", external_id="f1", description=_REAL_JD)
    pipeline.judge_one(jid1)

    with db.connect() as conn:
        result = {c["name"]: c["last_result"] for c in db.list_target_companies(conn)}
        assert result["Flaky Co"] == pipeline._PROBE_RETRY_RESULT      # not recorded as a miss
    assert ("probe_failed", "Flaky Co") in _drop_reasons()

    with db.connect() as conn:
        jid2 = _insert(conn, config, company="Flaky Co", external_id="f2", description=_REAL_JD)
    pipeline.judge_one(jid2)
    assert probed == ["Flaky Co", "Flaky Co"]                          # retried
    with db.connect() as conn:
        result = {c["name"]: c["last_result"] for c in db.list_target_companies(conn)}
    assert result["Flaky Co"] == "no ATS match found (auto-probed)"

    with db.connect() as conn:
        jid3 = _insert(conn, config, company="Flaky Co", external_id="f3", description=_REAL_JD)
    pipeline.judge_one(jid3)
    assert probed == ["Flaky Co", "Flaky Co"]                          # definite miss: stops for good


def test_judge_one_does_not_probe_a_company_already_on_the_manual_checklist(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("good"))
    probed = []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: probed.append(company))
    with db.connect() as conn:
        db.add_target_company(conn, "Manual Co")
        row = conn.execute("SELECT id FROM target_companies WHERE name = 'Manual Co'").fetchone()
        db.mark_company_checked(conn, row["id"], "careers page is a custom site, no ATS")
        jid = _insert(conn, config, company="Manual Co", description=_REAL_JD)

    pipeline.judge_one(jid)

    assert probed == []


def test_rejudge_juniors_unfilters_junior_rule_filtered_job(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rj1", description=_LONG_REAL_JD)
        conn.execute(
            "UPDATE jobs SET filtered=1, filter_reason='score<20', seniority='junior' WHERE id=?",
            (jid,))

    summary = pipeline.rejudge_juniors()

    assert summary["rescreened"] == 1
    assert summary["unfiltered"] == 1
    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["filtered"] == 0
    assert row["filter_reason"] == ""


def test_rejudge_juniors_leaves_non_junior_score_filtered_job_alone(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rj2", description=_LONG_REAL_JD)
        conn.execute(
            "UPDATE jobs SET filtered=1, filter_reason='score<20', seniority='unknown' WHERE id=?",
            (jid,))

    summary = pipeline.rejudge_juniors()

    assert summary["rescreened"] == 0   # not selected -- not junior
    with db.connect() as conn:
        assert db.get_job(conn, jid)["filtered"] == 1


def test_rejudge_juniors_rejudges_weak_junior_job_and_unfilters_on_improved_verdict(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rj3", description=_LONG_REAL_JD)
        conn.execute(
            "UPDATE jobs SET llm_verdict='weak', llm_score=10, seniority='junior', "
            "filtered=1, filter_reason='llm judge: weak fit' WHERE id=?", (jid,))
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("stretch"))

    summary = pipeline.rejudge_juniors()

    assert summary["rejudged"] == 1
    assert summary["unfiltered"] == 1
    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["llm_verdict"] == "stretch"
    assert row["filtered"] == 0


def test_rejudge_juniors_unfilters_when_old_reason_is_a_stale_score_gate(tmp_db, config, monkeypatch):
    # Real-world case: a job judged 'weak' long ago, then later rescreened (e.g. by
    # enrich_one) which overwrote filter_reason with just the rule-score flag,
    # losing the "llm judge: weak fit" trace even though llm_verdict is still 'weak'.
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rj7", description=_LONG_REAL_JD)
        conn.execute(
            "UPDATE jobs SET llm_verdict='weak', llm_score=33, seniority='junior', "
            "filtered=1, filter_reason='score<20' WHERE id=?", (jid,))
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("stretch"))

    summary = pipeline.rejudge_juniors()

    assert summary["unfiltered"] == 1
    with db.connect() as conn:
        assert db.get_job(conn, jid)["filtered"] == 0


def test_rejudge_juniors_does_not_unfilter_if_also_filtered_for_another_reason(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rj4", description=_LONG_REAL_JD)
        conn.execute(
            "UPDATE jobs SET llm_verdict='weak', llm_score=5, seniority='junior', filtered=1, "
            "filter_reason='citizenship/eligibility requirement; llm judge: weak fit' WHERE id=?",
            (jid,))
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("stretch"))

    summary = pipeline.rejudge_juniors()

    assert summary["rejudged"] == 1
    assert summary["unfiltered"] == 0   # combined reason -- left filtered, not this function's call
    with db.connect() as conn:
        assert db.get_job(conn, jid)["filtered"] == 1


def test_rejudge_juniors_skips_dismissed_and_interested_labels(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        dismissed = _insert(conn, config, external_id="rj5", description=_LONG_REAL_JD)
        conn.execute("UPDATE jobs SET llm_verdict='weak', seniority='junior', filtered=1, "
                     "filter_reason='llm judge: weak fit', user_label='dismissed' WHERE id=?",
                     (dismissed,))
        interested = _insert(conn, config, external_id="rj6", description=_LONG_REAL_JD)
        conn.execute("UPDATE jobs SET filtered=1, filter_reason='score<20', seniority='junior', "
                     "user_label='interested' WHERE id=?", (interested,))
    called = []
    monkeypatch.setattr(pipeline.llm_judge, "judge",
                        lambda job, preferences="": called.append(1) or _fake_verdict("stretch")(job))

    summary = pipeline.rejudge_juniors()

    assert summary == {"rescreened": 0, "rejudged": 0, "unfiltered": 0}
    assert called == []


def test_rejudge_juniors_respects_limit(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        for i in range(3):
            jid = _insert(conn, config, external_id=f"rj-limit-{i}", company=f"LimitCo{i}",
                         description=_LONG_REAL_JD)
            conn.execute("UPDATE jobs SET llm_verdict='weak', seniority='junior', filtered=1, "
                         "filter_reason='llm judge: weak fit' WHERE id=?", (jid,))
    monkeypatch.setattr(pipeline.llm_judge, "judge", _fake_verdict("stretch"))

    summary = pipeline.rejudge_juniors(limit=2)

    assert summary["rejudged"] == 2


def test_rejudge_category_rejudges_weak_verdicts_in_that_category_only(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        cv_jid = _insert(conn, config, external_id="rc1", company="CVCo", description=_LONG_REAL_JD)
        conn.execute("UPDATE jobs SET llm_verdict='weak', role_category='CV', filtered=1, "
                     "filter_reason='llm judge: weak fit' WHERE id=?", (cv_jid,))
        other_jid = _insert(conn, config, external_id="rc2", company="OtherCo", description=_LONG_REAL_JD)
        conn.execute("UPDATE jobs SET llm_verdict='weak', role_category='ML/DL', filtered=1, "
                     "filter_reason='llm judge: weak fit' WHERE id=?", (other_jid,))
    called = []
    monkeypatch.setattr(pipeline.llm_judge, "judge",
                        lambda job, preferences="": called.append(job.external_id) or
                        {"score": 60, "verdict": "stretch", "seniority": "junior",
                         "min_years": 0, "reasons": "ok now"})

    summary = pipeline.rejudge_category("CV")

    assert summary == {"rejudged": 1, "unfiltered": 1}
    assert called == ["rc1"]   # the ML/DL job was never touched
    with db.connect() as conn:
        assert db.get_job(conn, cv_jid)["filtered"] == 0
        assert db.get_job(conn, other_jid)["filtered"] == 1


def test_rejudge_category_skips_dismissed_and_interested_labels(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rc3", description=_LONG_REAL_JD)
        conn.execute("UPDATE jobs SET llm_verdict='weak', role_category='CV', filtered=1, "
                     "filter_reason='llm judge: weak fit', user_label='interested' WHERE id=?",
                     (jid,))
    called = []
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="": called.append(1))

    summary = pipeline.rejudge_category("CV")

    assert summary == {"rejudged": 0, "unfiltered": 0}
    assert called == []


def test_rescreen_all_updates_pre_judgment_job_and_can_unfilter(tmp_db, config):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rs1", title="Machine Learning Engineer",
                     description="We build segmentation models using point cloud data.")
        # Seed a stale score/filtered/category as if screened before the config changed.
        conn.execute("UPDATE jobs SET score=10, filtered=1, filter_reason='score<20', "
                     "role_category='ML/DL' WHERE id=?", (jid,))

    summary = pipeline.rescreen_all()

    assert summary["rescreened"] == 1
    assert summary["unfiltered"] == 1
    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["score"] > 10
    assert row["filtered"] == 0
    assert row["role_category"] == "CV"


def test_rescreen_all_only_relabels_already_judged_jobs(tmp_db, config):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="rs2", title="Machine Learning Engineer",
                     description="We build segmentation models using point cloud data.")
        conn.execute("UPDATE jobs SET score=10, filtered=1, filter_reason='llm judge: weak fit', "
                     "role_category='ML/DL', llm_score=15, llm_verdict='weak' WHERE id=?", (jid,))

    summary = pipeline.rescreen_all()

    assert summary["recategorized"] == 1
    assert summary["rescreened"] == 0   # not touched -- only pre-judgment jobs get a full rescreen
    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["score"] == 10           # untouched: LLM verdict is authoritative here
    assert row["filtered"] == 1         # untouched
    assert row["role_category"] == "CV"   # only the label refreshed


def test_rescreen_all_is_noop_when_nothing_changed(tmp_db, config):
    with db.connect() as conn:
        _insert(conn, config, external_id="rs3", description=_LONG_REAL_JD)

    summary = pipeline.rescreen_all()

    assert summary["rescreened"] == 0
    assert summary["recategorized"] == 0


def test_process_backlog_judges_and_tailors_the_whole_backlog(tmp_db, config, monkeypatch):
    """Unlike daily_run, process_backlog isn't scoped to "new this run" -- these
    jobs are pre-existing DB rows, inserted directly (bypassing _gather/run_fetch
    entirely), which is exactly the "stuck backlog" scenario this exists for."""
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: None)
    with db.connect() as conn:
        strong_jid = _insert(conn, config, external_id="10", company="StrongCo", description=_LONG_REAL_JD)
        weak_jid = _insert(conn, config, external_id="11", company="WeakCo", description=_LONG_REAL_JD)

    verdicts = {"StrongCo": "strong", "WeakCo": "weak"}

    def fake_judge(job, preferences=""):
        v = verdicts[job.company]
        return {"score": 10 if v == "weak" else 80, "verdict": v,
                "seniority": "junior", "min_years": 0, "reasons": "r"}

    monkeypatch.setattr(pipeline.llm_judge, "judge", fake_judge)
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw: None)
    tailored_ids = []
    monkeypatch.setattr(pipeline, "tailor_one",
                        lambda jid, auto=False: tailored_ids.append(jid) or {"job_id": jid, "compiled": True})
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: {"job_id": jid})

    summary = pipeline.process_backlog(judge_min_score=0, judge_limit=10, tailor_limit=10)

    assert summary["judged"] == 2
    assert summary["tailored"] == 1
    assert tailored_ids == [strong_jid]   # not the weak-verdict job


def test_process_backlog_retries_enrichment_for_stuck_jobs(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="20", description="")   # too short, never enriched
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _LONG_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 80, "verdict": "weak", "seniority": "junior",
                         "min_years": 0, "reasons": "r"})

    summary = pipeline.process_backlog(judge_min_score=0)

    assert summary["enriched"] == 1
    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["description_full"] == 1


def test_enrich_one_bumps_attempts_on_failure_and_leaves_them_on_success(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        broken = _insert(conn, config, external_id="21", company="BrokenCo", description="")
        fine = _insert(conn, config, external_id="22", company="FineCo", description="")

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: None)
    result = pipeline.enrich_one(broken)
    assert result == {"job_id": broken, "enriched": False}
    with db.connect() as conn:
        assert db.get_job(conn, broken)["enrich_attempts"] == 1

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _LONG_REAL_JD)
    pipeline.enrich_one(fine)
    with db.connect() as conn:
        assert db.get_job(conn, fine)["enrich_attempts"] == 0   # success: counter untouched


def test_process_backlog_gives_up_on_a_permanently_broken_job(tmp_db, config, monkeypatch):
    # Regression test for the real incident: process_backlog always pulls the
    # *oldest* unenriched jobs first, so a job whose source can never be
    # enriched (delisted, blocked, etc.) would otherwise occupy every retry
    # slot forever and starve every newer job of a turn.
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        broken = _insert(conn, config, external_id="30", company="BrokenCo", description="")  # oldest
        for _ in range(db.MAX_ENRICH_ATTEMPTS):
            db.bump_enrich_attempts(conn, broken)
        newer = _insert(conn, config, external_id="31", company="NewerCo", description="")    # would be starved

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _LONG_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 80, "verdict": "weak", "seniority": "junior",
                         "min_years": 0, "reasons": "r"})

    summary = pipeline.process_backlog(judge_min_score=0, judge_limit=1)

    assert summary["enriched"] == 1
    with db.connect() as conn:
        assert db.get_job(conn, newer)["description_full"] == 1     # got its turn
        assert db.get_job(conn, broken)["description_full"] == 0    # correctly left alone


def test_process_backlog_noop_when_provider_unavailable(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: False)
    called = []
    monkeypatch.setattr(pipeline, "judge_all", lambda **k: called.append(1))

    summary = pipeline.process_backlog()

    assert summary == {"enriched": 0, "judged": 0, "skipped_no_description": 0, "tailored": 0,
                       "dup_checked": 0, "dup_filtered": 0}
    assert called == []


def _insert_dup_pair(conn, config, description_a=_LONG_REAL_JD, description_b=_LONG_REAL_JD):
    """Two jobs the heuristic (find_possible_duplicates) will flag as a possible dup:
    same company, same city, identical title once gender/contract boilerplate (H/F) is
    stripped. The titles must NOT be byte-for-byte identical after plain normalization
    (only after de-junking), or upsert_job's own cross-source dedup (_find_content_match,
    which doesn't strip junk tokens) would silently merge them into a single row instead
    of creating the two separate rows this test needs."""
    a = _insert(conn, config, external_id="dup-a", company="DupCo", title="AI Engineer (H/F)",
               location="Paris, Ile-de-France, France", description=description_a)
    b = _insert(conn, config, external_id="dup-b", company="DupCo", title="AI Engineer",
               location="Paris, Ile-de-France, France", description=description_b)
    return a, b


def test_check_duplicates_skips_pair_missing_jd_content(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        _insert_dup_pair(conn, config, description_b="too short")
    called = []
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: called.append(1))

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 0, "same": 0, "filtered": 0, "by_rule": 0}
    assert called == []


def test_check_duplicates_caches_verdict_and_never_rechecks(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        a, b = _insert_dup_pair(conn, config)
    calls = []

    def fake_compare(job_a, job_b):
        calls.append((job_a.external_id, job_b.external_id))
        return {"verdict": "different", "confidence": "low", "reason": "distinct teams"}

    monkeypatch.setattr(pipeline.llm_dedup, "compare", fake_compare)

    stats = pipeline.check_duplicates()
    assert stats == {"checked": 1, "same": 0, "filtered": 0, "by_rule": 0}
    assert len(calls) == 1
    with db.connect() as conn:
        assert db.get_duplicate_check(conn, a, b)["verdict"] == "different"

    pipeline.check_duplicates()   # second call: pair already cached, no re-check
    assert len(calls) == 1


def test_check_duplicates_auto_filters_older_job_on_confident_same_verdict(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        older, newer = _insert_dup_pair(conn, config)
        conn.execute("UPDATE jobs SET fetched_at = '2020-01-01 00:00:00' WHERE id = ?", (older,))
        conn.execute("UPDATE jobs SET fetched_at = '2030-01-01 00:00:00' WHERE id = ?", (newer,))
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b:
                        {"verdict": "same", "confidence": "high", "reason": "identical JD"})

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 1, "same": 1, "filtered": 1, "by_rule": 0}
    with db.connect() as conn:
        assert db.get_job(conn, older)["filtered"] == 1
        assert db.get_job(conn, newer)["filtered"] == 0   # the newer listing is kept


def test_check_duplicates_does_not_filter_on_low_confidence_same_verdict(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        a, b = _insert_dup_pair(conn, config)
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b:
                        {"verdict": "same", "confidence": "medium", "reason": "looks similar"})

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 1, "same": 0, "filtered": 0, "by_rule": 0}
    with db.connect() as conn:
        assert db.get_job(conn, a)["filtered"] == 0
        assert db.get_job(conn, b)["filtered"] == 0


_SHARED_TEXT = " ".join(f"shared{i}" for i in range(60))       # two listings sharing this overlap by ~30%


def _partly_shared(own: str) -> str:
    return _SHARED_TEXT + " " + " ".join(f"{own}{i}" for i in range(60))


def test_two_different_roles_with_loosely_overlapping_text_are_settled_without_the_llm(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: 1 / 0)
    with db.connect() as conn:
        a = _insert(conn, config, external_id="r-a", company="RuleCo", title="Data Analyst",
                    description=_partly_shared("alpha"))
        b = _insert(conn, config, external_id="r-b", company="RuleCo", title="Machine Learning Engineer",
                    description=_partly_shared("beta"))

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 1, "same": 0, "filtered": 0, "by_rule": 1}
    with db.connect() as conn:
        check = db.get_duplicate_check(conn, a, b)
        assert check["verdict"] == "different" and check["reason"].startswith("rule:")


def test_the_same_title_in_the_same_city_with_shared_text_is_a_duplicate_without_the_llm(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: False)
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: 1 / 0)
    with db.connect() as conn:
        older = _insert(conn, config, external_id="s-a", company="RuleCo", title="Data Scientist (H/F)",
                        description=_partly_shared("alpha"))
        newer = _insert(conn, config, external_id="s-b", company="RuleCo", title="Data Scientist",
                        description=_partly_shared("beta"))
        conn.execute("UPDATE jobs SET fetched_at = '2020-01-01 00:00:00' WHERE id = ?", (older,))
        conn.execute("UPDATE jobs SET fetched_at = '2030-01-01 00:00:00' WHERE id = ?", (newer,))
        conn.execute("UPDATE jobs SET filtered = 0, filter_reason = '' WHERE id IN (?, ?)", (older, newer))

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 1, "same": 1, "filtered": 1, "by_rule": 1}
    with db.connect() as conn:
        assert db.get_job(conn, older)["filtered"] == 1 and db.get_job(conn, newer)["filtered"] == 0


def test_rule_settled_pairs_do_not_use_up_the_llm_limit(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    calls = []
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: calls.append(1) or
                        {"verdict": "different", "confidence": "high", "reason": "x"})
    with db.connect() as conn:
        _insert(conn, config, external_id="l-a", company="RuleCo", title="Data Analyst",
                description=_partly_shared("alpha"))
        _insert(conn, config, external_id="l-b", company="RuleCo", title="Machine Learning Engineer",
                description=_partly_shared("beta"))
        _insert_dup_pair(conn, config)                      # needs the LLM: same title, no shared text

    stats = pipeline.check_duplicates(limit=1)

    assert stats["by_rule"] == 1 and stats["checked"] == 2 and len(calls) == 1


_SAME_POSTING = ("We build a production LLM platform and need an engineer to design agentic pipelines, "
                 "evaluate models, fine-tune them and ship them to customers with a small team. ") * 3


def _insert_listings(conn, config, cities=("Nantes", "Lyon"), **kw):
    """The same opening listed once per city (identical text, near-identical title)."""
    ids = [_insert(conn, config, external_id=f"{cities[0]}-{i}", company="MultiCo", location=city,
                   title="AI Engineer" if i == 0 else "AI Engineer (H/F)", description=_SAME_POSTING, **kw)
           for i, city in enumerate(cities)]
    conn.execute("UPDATE jobs SET filtered = 0, filter_reason = '' WHERE id IN (%s)" % ",".join(map(str, ids)))
    return ids


def test_identical_text_is_the_same_posting_without_asking_the_llm(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: False)     # no LLM at all
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: 1 / 0)
    with db.connect() as conn:
        first, second = _insert_listings(conn, config)
        conn.execute("UPDATE jobs SET fetched_at = '2020-01-01 00:00:00' WHERE id = ?", (first,))
        conn.execute("UPDATE jobs SET fetched_at = '2030-01-01 00:00:00' WHERE id = ?", (second,))

    stats = pipeline.check_duplicates()

    assert stats == {"checked": 1, "same": 1, "filtered": 1, "by_rule": 0}
    with db.connect() as conn:
        assert db.get_job(conn, first)["filtered"] == 1 and db.get_job(conn, second)["filtered"] == 0
        reason = db.get_job(conn, first)["filter_reason"]
        assert f"duplicate of #{second}" in reason and "identical" in reason
        assert db.get_duplicate_check(conn, first, second)["verdict"] == "same"


def test_a_job_you_applied_to_is_the_original_and_the_newer_copy_is_hidden(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        applied, fresh = _insert_listings(conn, config)
        db.update_status(conn, applied, "applied")
        conn.execute("UPDATE jobs SET fetched_at = '2020-01-01 00:00:00' WHERE id = ?", (applied,))
        conn.execute("UPDATE jobs SET fetched_at = '2030-01-01 00:00:00' WHERE id = ?", (fresh,))

    pipeline.check_duplicates()

    with db.connect() as conn:
        assert db.get_job(conn, applied)["filtered"] == 0                  # never hidden, although older
        assert db.get_job(conn, fresh)["filtered"] == 1
        assert f"duplicate of #{applied} (applied)" in db.get_job(conn, fresh)["filter_reason"]


def test_two_listings_you_both_acted_on_are_both_kept(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        a, b = _insert_listings(conn, config)
        db.update_status(conn, a, "applied")
        db.update_status(conn, b, "rejected")

    stats = pipeline.check_duplicates()

    assert stats["same"] == 1 and stats["filtered"] == 0
    with db.connect() as conn:
        assert db.get_job(conn, a)["filtered"] == 0 and db.get_job(conn, b)["filtered"] == 0


def test_pairs_involving_a_job_in_play_are_checked_first_when_the_limit_is_tight(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    asked = []
    monkeypatch.setattr(pipeline.llm_dedup, "compare", lambda a, b: asked.append(a.company) or
                        {"verdict": "different", "confidence": "high", "reason": "x"})
    with db.connect() as conn:
        _insert_dup_pair(conn, config)                                      # DupCo: nobody acted on it
        for i in range(2):
            j = _insert(conn, config, external_id=f"p{i}", company="PlayCo", description=_LONG_REAL_JD,
                        title="ML Engineer (H/F)" if i else "ML Engineer")
            if i:
                db.update_status(conn, j, "cv_ready")

    pipeline.check_duplicates(limit=1)

    assert asked == ["PlayCo"]


def _visible(conn, ids):
    return sorted(i for i in ids if not db.get_job(conn, i)["filtered"])


def test_several_listings_of_one_opening_leave_exactly_one_visible_whatever_the_order(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: False)
    with db.connect() as conn:
        ids = _insert_listings(conn, config, cities=("Nantes", "Lyon", "Rennes", "Rouen"))
        db.update_status(conn, ids[1], "cv_ready")

    pipeline.check_duplicates()

    with db.connect() as conn:
        assert _visible(conn, ids) == [ids[1]]                  # the one with a CV stays, the rest are hidden
        for jid in ids:
            if jid != ids[1]:
                assert f"duplicate of #{ids[1]} (cv_ready)" in db.get_job(conn, jid)["filter_reason"]


def test_a_copy_is_never_hidden_when_its_original_is_already_hidden(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: False)
    with db.connect() as conn:
        first, second = _insert_listings(conn, config)
        db.update_status(conn, first, "cv_ready")                    # would be the original...
        db.set_filtered(conn, first, True, "outside the target area")  # ...but a rule hid it

    pipeline.check_duplicates()

    with db.connect() as conn:
        assert db.get_job(conn, second)["filtered"] == 0           # one copy stays visible


def test_repair_restores_an_applied_job_hidden_by_the_old_rule_and_hides_the_new_copy(tmp_db, config):
    with db.connect() as conn:
        applied, fresh = _insert_listings(conn, config)
        db.update_status(conn, applied, "applied")
        db.record_duplicate_check(conn, applied, fresh, "same", "high", "identical")
        db.set_llm_filter(conn, applied, f"llm dedup: same posting as #{fresh} -- identical")   # the old rule

    plan = pipeline.repair_duplicates()                                    # a dry run changes nothing
    assert applied not in plan["hide"] and plan["hide"] == {fresh: applied}
    assert applied in plan["restore"]
    with db.connect() as conn:
        assert db.get_job(conn, applied)["filtered"] == 1 and db.get_job(conn, fresh)["filtered"] == 0

    pipeline.repair_duplicates(apply=True)

    with db.connect() as conn:
        assert db.get_job(conn, applied)["filtered"] == 0
        assert db.get_job(conn, fresh)["filtered"] == 1
        assert f"duplicate of #{applied} (applied)" in db.get_job(conn, fresh)["filter_reason"]


def test_repair_restores_copies_hidden_when_the_original_was_hidden_too_and_keeps_hand_written_reasons(tmp_db, config):
    with db.connect() as conn:
        a, b = _insert_listings(conn, config)
        db.record_duplicate_check(conn, a, b, "same", "high", "identical")
        db.set_filtered(conn, a, True, "outside the target area")
        db.set_llm_filter(conn, b, f"duplicate of #{a} (new) -- same posting")        # both copies now hidden
        manual, other = _insert_listings(conn, config, cities=("Brest", "Tours"))
        db.set_filtered(conn, manual, True, "duplicate of #999 -- same role, applied elsewhere")   # by hand

    plan = pipeline.repair_duplicates(apply=True)

    assert b in plan["restore"] and manual not in plan["restore"]
    with db.connect() as conn:
        assert db.get_job(conn, b)["filtered"] == 0
        assert db.get_job(conn, manual)["filtered"] == 1


def test_refit_one_rebuilds_the_newest_cv_from_its_saved_plan_without_asking_the_llm(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    tex = tmp_path / "cv-old.tex"
    tex.write_text("x")
    tex.with_suffix(".plan.json").write_text(
        '{"language": "fr", "role_category": "AI", "selection": {"experience_ids": [0]}, '
        '"summary": {"text": "t", "reason": "", "detail": ""}}')
    with db.connect() as conn:
        db.add_cv_artifact(conn, jid, str(tex), str(tmp_path / "cv-old.pdf"), origin="ai", lang="fr")
    seen = {}

    def fake_tailor_job(job, job_id, auto=False, judge_context=None, role_category="", language=None, stored=None):
        seen.update(language=language, stored=stored, auto=auto)
        return _fake_tailor_job(tmp_path)(job, job_id, auto=auto)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake_tailor_job)

    result = pipeline.refit_one(jid)

    assert result["compiled"] and seen["language"] == "fr" and seen["auto"] is True
    assert seen["stored"]["selection"] == {"experience_ids": [0]}


def test_refit_one_skips_a_job_without_a_saved_plan_or_with_a_hand_revised_cv(tmp_db, config, tmp_path):
    with db.connect() as conn:
        bare = _insert(conn, config, external_id="bare", description=_REAL_JD)
        revised = _insert(conn, config, external_id="rev", company="Other", description=_REAL_JD)
        tex = tmp_path / "cv-x.tex"
        tex.write_text("x")
        db.add_cv_artifact(conn, bare, str(tex), str(tmp_path / "cv-x.pdf"), origin="ai")
        tex.with_suffix(".plan.json").write_text('{"selection": {"a": 1}, "summary": {"text": "t"}}')
        db.add_cv_artifact(conn, revised, str(tex), str(tmp_path / "rev.pdf"), origin="revised")
        tex.with_suffix(".plan.json").unlink()

    assert "no saved selection" in pipeline.refit_one(bare)["skipped"]
    assert "no saved selection" in pipeline.refit_one(revised)["skipped"]
    assert pipeline.refit_one(999999) == {"job_id": 999999, "error": "not found"}


def test_process_backlog_settles_duplicates_before_it_tailors(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    order = []
    monkeypatch.setattr(pipeline, "judge_all", lambda **k: {"judged": 0, "skipped_no_description": 0})
    monkeypatch.setattr(pipeline, "check_duplicates", lambda limit=10: order.append("dedup") or
                        {"checked": 0, "same": 0, "filtered": 0})
    monkeypatch.setattr(pipeline, "_auto_tailor_jobs", lambda ids, limit: order.append("tailor") or 0)

    pipeline.process_backlog()

    assert order == ["dedup", "tailor"]


def test_a_duplicate_hidden_before_tailoring_never_gets_a_cv(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    with db.connect() as conn:
        applied, fresh = _insert_listings(conn, config)
        db.update_status(conn, applied, "applied")
        for jid in (applied, fresh):
            db.set_llm_judgment(conn, jid, 80, "good", "r")
    tailored = []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False, language=None:
                        tailored.append(jid) or {"compiled": False})
    monkeypatch.setattr(pipeline, "judge_all", lambda **k: {"judged": 0, "skipped_no_description": 0})

    pipeline.process_backlog()

    assert fresh not in tailored


def test_tailor_one_and_cover_one_pass_the_judge_context_through(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.set_llm_judgment(conn, jid, 89, "strong", "great domain match")

    captured = {}
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job",
                        lambda job, job_id, auto=False, judge_context=None, role_category="", language=None:
                        captured.update(tailor_ctx=judge_context) or cv_engine.TailorResult(Path("/tmp/cv.tex"), Path("/tmp/cv.pdf")))
    monkeypatch.setattr(pipeline.cover_letter, "draft_to_file",
                        lambda job, out_dir, judge_context=None, cv_text=None, language="en":
                        captured.update(cover_ctx=judge_context) or Path("/tmp/cover_letter.md"))

    pipeline.tailor_one(jid)
    pipeline.cover_one(jid)

    expected = "Rated 'strong' fit (89/100): great domain match"
    assert captured["tailor_ctx"] == expected
    assert captured["cover_ctx"] == expected


def test_judge_context_is_none_when_job_not_yet_judged(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)

    captured = {}
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job",
                        lambda job, job_id, auto=False, judge_context=None, role_category="", language=None:
                        captured.update(tailor_ctx=judge_context) or cv_engine.TailorResult(Path("/tmp/cv.tex"), Path("/tmp/cv.pdf")))

    pipeline.tailor_one(jid)

    assert captured["tailor_ctx"] is None


def _fake_tailor_job(tmp_path, note=""):
    """A tailor_job stand-in that writes real versioned files, like the engine does."""
    calls = []

    def fake(job, job_id, auto=False, judge_context=None, role_category="", language=None, stored=None):
        stamp = f"v{len(calls)}"
        tex, pdf = tmp_path / f"cv-{stamp}.tex", tmp_path / f"cv-{stamp}.pdf"
        tex.write_text("tex"); pdf.write_bytes(b"%PDF")
        calls.append(job_id)
        return cv_engine.TailorResult(tex, pdf, note)
    fake.calls = calls
    return fake


def _write_cv(tmp_path, name, marker):
    tex = tmp_path / f"{name}.tex"
    tex.write_text(f"\\begin{{document}}{marker}\\end{{document}}", encoding="utf-8")
    return str(tex)


def _letter_cv_text(tmp_db, config, monkeypatch, artifacts):
    """Run cover_one on a job with the given artifacts (oldest first); return the cv_text it used."""
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        for tex, pdf, origin in artifacts:
            db.add_cv_artifact(conn, jid, tex, pdf, origin=origin)
    captured = {}
    monkeypatch.setattr(pipeline.cover_letter, "draft_to_file",
                        lambda job, out_dir, judge_context=None, cv_text=None, language="en":
                        captured.update(cv_text=cv_text) or Path("/tmp/cover_letter.md"))
    pipeline.cover_one(jid)
    return captured["cv_text"]


def test_cover_one_uses_the_newest_tailored_cv(tmp_db, config, tmp_path, monkeypatch):
    text = _letter_cv_text(tmp_db, config, monkeypatch, [
        (_write_cv(tmp_path, "cv-1", "OLD_VERSION"), "/t/1.pdf", "ai"),
        (_write_cv(tmp_path, "cv-2", "NEW_VERSION"), "/t/2.pdf", "ai")])
    assert "NEW_VERSION" in text and "OLD_VERSION" not in text


def test_cover_one_skips_a_failed_newest_attempt_for_the_older_good_cv(tmp_db, config, tmp_path, monkeypatch):
    text = _letter_cv_text(tmp_db, config, monkeypatch, [
        (_write_cv(tmp_path, "cv-1", "GOOD_VERSION"), "/t/1.pdf", "ai"),
        (_write_cv(tmp_path, "cv-2", "FAILED_VERSION"), "", "ai")])   # no PDF: failed compile
    assert "GOOD_VERSION" in text


def test_cover_one_uses_the_ai_tex_when_the_newest_cv_is_a_pdf_only_upload(tmp_db, config, tmp_path, monkeypatch):
    text = _letter_cv_text(tmp_db, config, monkeypatch, [
        (_write_cv(tmp_path, "cv-1", "AI_VERSION"), "/t/1.pdf", "ai"),
        ("", "/t/revised.pdf", "revised")])
    assert "AI_VERSION" in text


def test_cover_one_falls_back_to_the_base_cv_when_there_is_no_usable_tailored_cv(tmp_db, config, monkeypatch):
    assert _letter_cv_text(tmp_db, config, monkeypatch, []) is None


def test_auto_tailor_drafts_the_letter_only_for_a_properly_tailored_cv(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        good = _insert(conn, config, external_id="g", company="G", description=_REAL_JD)
        fallback = _insert(conn, config, external_id="f", company="F", description=_REAL_JD)
        failed = _insert(conn, config, external_id="x", company="X", description=_REAL_JD)
    results = {
        good: {"compiled": True, "note": "", "fallback": False},
        fallback: {"compiled": True, "note": db.CV_FALLBACK_NOTE, "fallback": True},
        failed: {"compiled": False, "note": "compiled to 3 page(s)", "fallback": False},
    }
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False: results[jid])
    letters = []
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: letters.append(jid) or {})

    tailored = pipeline._auto_tailor_jobs([good, fallback, failed], limit=10)

    assert letters == [good]
    assert tailored == 2                 # the fallback CV compiled, the failed one did not


def test_tailor_lock_is_exclusive_and_released_afterwards(tmp_path):
    with pipeline._tailor_lock(tmp_path) as first:
        assert first
        with pipeline._tailor_lock(tmp_path) as second:
            assert not second                      # a concurrent run is refused, not blocked
    with pipeline._tailor_lock(tmp_path) as again:
        assert again


def test_tailor_one_skips_a_job_another_run_is_tailoring(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    fake = _fake_tailor_job(tmp_path)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake)
    out_dir = pipeline.cv_engine.CV_OUT_DIR / f"{jid}-{pipeline.cv_engine._slug('Acme')}"

    with pipeline._tailor_lock(out_dir):           # the "other run"
        result = pipeline.tailor_one(jid, auto=True)

    assert result["skipped"] and fake.calls == []
    with db.connect() as conn:
        assert db.list_cv_artifacts(conn, jid) == []


def test_tailor_one_auto_rechecks_under_the_lock_but_manual_tailoring_still_runs(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.add_cv_artifact(conn, jid, "/t/1.tex", "/t/1.pdf")        # another run finished it meanwhile
    fake = _fake_tailor_job(tmp_path)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake)

    assert pipeline.tailor_one(jid, auto=True)["skipped"] == "already tailored"
    assert fake.calls == []
    assert "skipped" not in pipeline.tailor_one(jid)                  # the dashboard/CLI re-tailor button
    assert fake.calls == [jid]


def test_auto_tailor_moves_on_when_a_job_is_skipped_by_the_lock(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False: {"job_id": jid, "skipped": "busy"})
    letters = []
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: letters.append(jid) or {})

    assert pipeline._auto_tailor_jobs([jid], limit=10) == 0
    assert letters == []


def test_tailor_one_records_a_keyword_fallback_cv_with_its_marker(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", _fake_tailor_job(tmp_path, db.CV_FALLBACK_NOTE))

    result = pipeline.tailor_one(jid, auto=True)

    assert result["fallback"] and result["compiled"] and not result["unchanged"]
    with db.connect() as conn:
        assert db.list_cv_artifacts(conn, jid)[0]["note"] == db.CV_FALLBACK_NOTE
        assert db.get_job(conn, jid)["status"] == "cv_ready"      # usable immediately
        assert db.needs_tailoring(conn, jid)                       # ...but still due an upgrade


@pytest.mark.parametrize("status", ["applied", "rejected", "unavailable", "interview"])
def test_re_tailoring_keeps_a_job_past_cv_ready_where_it_is(tmp_db, config, tmp_path, monkeypatch, status):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.update_status(conn, jid, status)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", _fake_tailor_job(tmp_path))

    result = pipeline.tailor_one(jid, auto=False)

    assert result["compiled"]
    with db.connect() as conn:
        assert db.get_job(conn, jid)["status"] == status
        assert len(db.list_cv_artifacts(conn, jid)) == 1          # the new version is still recorded


def test_re_tailoring_a_shortlisted_job_makes_it_cv_ready(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.update_status(conn, jid, "shortlisted")
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", _fake_tailor_job(tmp_path))

    pipeline.tailor_one(jid, auto=False)

    with db.connect() as conn:
        assert db.get_job(conn, jid)["status"] == "cv_ready"


def test_tailor_one_upgrades_a_fallback_cv_when_the_llm_is_back(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.add_cv_artifact(conn, jid, "/tmp/old.tex", "/tmp/old.pdf", note=db.CV_FALLBACK_NOTE)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", _fake_tailor_job(tmp_path))

    pipeline.tailor_one(jid, auto=True)

    with db.connect() as conn:
        assert len(db.list_cv_artifacts(conn, jid)) == 2
        assert not db.needs_tailoring(conn, jid)


def test_tailor_one_does_not_pile_up_repeat_fallback_cvs(tmp_db, config, tmp_path, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.add_cv_artifact(conn, jid, "/tmp/old.tex", "/tmp/old.pdf", note=db.CV_FALLBACK_NOTE)
    fake = _fake_tailor_job(tmp_path, db.CV_FALLBACK_NOTE)
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake)

    result = pipeline.tailor_one(jid, auto=True)

    assert result["unchanged"]
    with db.connect() as conn:
        assert len(db.list_cv_artifacts(conn, jid)) == 1          # no duplicate row
    assert not (tmp_path / "cv-v0.tex").exists() and not (tmp_path / "cv-v0.pdf").exists()


def test_auto_tailor_upgrades_fallback_cvs_but_skips_proper_ones(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        fallback = _insert(conn, config, external_id="a", company="A", description=_REAL_JD)
        db.add_cv_artifact(conn, fallback, "/t/a.tex", "/t/a.pdf", note=db.CV_FALLBACK_NOTE)
        proper = _insert(conn, config, external_id="b", company="B", description=_REAL_JD)
        db.add_cv_artifact(conn, proper, "/t/b.tex", "/t/b.pdf")
    tailored = []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False:
                        tailored.append(jid) or {"compiled": True, "note": "", "fallback": False})
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: {})

    pipeline._auto_tailor_jobs([fallback, proper], limit=10)

    assert tailored == [fallback]


def test_auto_tailor_stops_retrying_upgrades_once_the_llm_fails_again(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        up1 = _insert(conn, config, external_id="u1", company="U1", description=_REAL_JD)
        db.add_cv_artifact(conn, up1, "/t/1.tex", "/t/1.pdf", note=db.CV_FALLBACK_NOTE)
        up2 = _insert(conn, config, external_id="u2", company="U2", description=_REAL_JD)
        db.add_cv_artifact(conn, up2, "/t/2.tex", "/t/2.pdf", note=db.CV_FALLBACK_NOTE)
        fresh = _insert(conn, config, external_id="f", company="F", description=_REAL_JD)
    tailored = []
    monkeypatch.setattr(pipeline, "tailor_one", lambda jid, auto=False:
                        tailored.append(jid) or {"compiled": True, "note": db.CV_FALLBACK_NOTE, "fallback": True})
    monkeypatch.setattr(pipeline, "cover_one", lambda jid: {})

    pipeline._auto_tailor_jobs([up1, up2, fresh], limit=10)

    assert tailored == [up1, fresh]   # up2's upgrade skipped, but a job with no CV still gets one


# --- backfill: shared persist helper -----------------------------------------

def test_persist_jobs_does_not_write_fetch_runs(tmp_db, config):
    job = Job(source="arbeitnow", external_id="1", title="ML Engineer", company="Acme", location="Paris")
    with db.connect() as conn:
        kept = pipeline._persist_jobs(conn, config, [job])
        assert len(kept) == 1
        assert conn.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 0
    # run_fetch, by contrast, does log a fetch_runs row -- confirms the two
    # paths genuinely differ, not just that _persist_jobs happens to skip it.
    pipeline.run_fetch(config, jobs=[Job(source="arbeitnow", external_id="2", title="ML Engineer",
                                         company="Acme", location="Paris")])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 1


def test_persist_jobs_applies_active_filter_rule(tmp_db, config):
    with db.connect() as conn:
        db.add_rule(conn, "company_block", "badcorp", active=1)
        job = Job(source="arbeitnow", external_id="1", title="ML Engineer",
                 company="BadCorp", location="Paris")
        kept = pipeline._persist_jobs(conn, config, [job])
        assert len(kept) == 1
        jid = kept[0][3]
        assert db.get_job(conn, jid)["filtered"] == 1


# --- backfill: day-of-week group routing -------------------------------------

def _stub_all_backfills(monkeypatch, calls):
    stub = lambda name: lambda force=False: calls.append(name) or {"fetched": 0}
    monkeypatch.setattr(pipeline, "_BACKFILL_FUNCS",
                        {name: stub(name) for name in pipeline._BACKFILL_FUNCS})


def test_run_backfill_saturday_runs_only_its_group(monkeypatch):
    calls = []
    _stub_all_backfills(monkeypatch, calls)
    pipeline.run_backfill(day="saturday")
    assert set(calls) == {"arbeitnow", "wttj", "eures"}


def test_run_backfill_sunday_runs_only_its_group(monkeypatch):
    calls = []
    _stub_all_backfills(monkeypatch, calls)
    pipeline.run_backfill(day="sunday")
    assert set(calls) == {"workday", "francetravail", "linkedin_wide"}


def test_run_backfill_weekday_runs_nothing(monkeypatch):
    calls = []
    _stub_all_backfills(monkeypatch, calls)
    result = pipeline.run_backfill(day="tuesday")
    assert calls == [] and result == {}


def test_run_backfill_force_ignores_day_and_runs_everything(monkeypatch):
    calls = []
    _stub_all_backfills(monkeypatch, calls)
    pipeline.run_backfill(day="tuesday", force=True)
    assert set(calls) == set(pipeline._BACKFILL_FUNCS)


def test_run_backfill_one_source_failing_does_not_stop_others(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_path / "backfill.log")
    monkeypatch.setattr(pipeline, "_BACKFILL_FUNCS", {
        "arbeitnow": lambda force=False: (_ for _ in ()).throw(RuntimeError("boom")),
        "wttj": lambda force=False: {"fetched": 3},
    })
    result = pipeline.run_backfill(day="saturday")
    assert "error" in result["arbeitnow"]
    assert result["wttj"] == {"fetched": 3}


# --- backfill: francetravail resumable date-window walk ----------------------

def test_backfill_francetravail_first_run_starts_with_no_cursor(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    captured = {}
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda query, deps, before_date, window_days=90:
                        captured.update(before_date=before_date) or ([], "2026-01-01T00:00:00Z", True))
    pipeline.backfill_francetravail()
    assert captured["before_date"] is None


def test_backfill_francetravail_resumes_saved_cursor_when_due(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    with db.connect() as conn:
        db.record_backfill_progress(conn, "francetravail_backfill", "2026-03-01T00:00:00Z", False, 50)
        conn.execute("UPDATE source_fetch_state SET last_attempted_at = datetime('now', '-200 hours') "
                     "WHERE source = 'francetravail_backfill'")

    captured = {}
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda query, deps, before_date, window_days=90:
                        captured.update(before_date=before_date) or ([], "2026-02-01T00:00:00Z", False))
    pipeline.backfill_francetravail()
    assert captured["before_date"] == "2026-03-01T00:00:00Z"   # resumed, not restarted


def test_backfill_francetravail_skips_when_not_due(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    with db.connect() as conn:
        db.record_backfill_progress(conn, "francetravail_backfill", "2026-03-01T00:00:00Z", False, 50)
        # last_attempted_at defaults to "now" -- well under the 168h interval

    called = []
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda *a, **k: called.append(1) or ([], "", True))
    result = pipeline.backfill_francetravail()
    assert result == {"skipped": "not due yet"} and called == []


def test_backfill_francetravail_idles_once_caught_up(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    with db.connect() as conn:
        db.record_backfill_progress(conn, "francetravail_backfill", "", True, 0)   # done, just now

    called = []
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda *a, **k: called.append(1) or ([], "", True))
    result = pipeline.backfill_francetravail()
    assert result == {"skipped": "caught up, idling"} and called == []


def test_backfill_francetravail_rewalks_from_scratch_after_idle_period(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    with db.connect() as conn:
        db.record_backfill_progress(conn, "francetravail_backfill", "", True, 0)
        conn.execute("UPDATE source_fetch_state SET last_attempted_at = datetime('now', '-300 hours') "
                     "WHERE source = 'francetravail_backfill'")   # past the 7-day rewalk_after_days

    captured = {}
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda query, deps, before_date, window_days=90:
                        captured.update(before_date=before_date) or ([], "2026-09-01T00:00:00Z", False))
    pipeline.backfill_francetravail()
    assert captured["before_date"] is None   # restarted fresh, not resumed from ""


def test_backfill_francetravail_force_bypasses_due_check(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline, "BACKFILL_LOG_PATH", tmp_db.parent / "backfill.log")
    with db.connect() as conn:
        db.record_backfill_progress(conn, "francetravail_backfill", "2026-03-01T00:00:00Z", False, 50)

    called = []
    monkeypatch.setattr(pipeline.francetravail, "fetch_before",
                        lambda *a, **k: called.append(1) or ([], "", True))
    result = pipeline.backfill_francetravail(force=True)
    assert called   # one call per configured francetravail query, at least one
    assert "skipped" not in result


def test_enrich_one_failure_persists_the_fetch_diag_reason(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="41", company="DiagCo", description="")

    def failing_fetch(source, ext, url, client=None):
        fetch_diag.track(source, "enrich_bad_response", detail="HTTP 403 http://x")
        return None

    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", failing_fetch)
    pipeline.enrich_one(jid)

    with db.connect() as conn:
        rows = db.recent_fetch_drops(conn, hours=1)
    assert [(r["source"], r["reason"], r["count"]) for r in rows] == [("linkedin", "enrich_bad_response", 1)]
    assert json.loads(rows[0]["samples"]) == ["HTTP 403 http://x"]


def test_enrich_one_success_records_no_drops(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="42", company="FineCo", description="")
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _LONG_REAL_JD)
    pipeline.enrich_one(jid)
    with db.connect() as conn:
        assert db.recent_fetch_drops(conn, hours=1) == []


_FR_JD = ("Nous recherchons un ingénieur pour rejoindre notre équipe. Vous travaillerez avec les "
          "data scientists sur des projets de machine learning dans une entreprise en forte "
          "croissance, et vous serez en charge de la mise en production des modèles. ") * 2
_EN_JD = ("We are looking for an engineer to join our team. You will work with the data scientists "
          "on machine learning projects in a fast growing company, and you will be in charge of "
          "putting the models into production. ") * 2


def _language_of(jid):
    with db.connect() as conn:
        return db.get_job(conn, jid)["language"]


def test_enrich_one_stores_the_language_detected_from_the_text(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="fr1", description="", language="en")  # wrong label
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _FR_JD)
    pipeline.enrich_one(jid)
    assert _language_of(jid) == "fr"


def test_enrich_one_keeps_the_label_when_the_text_cannot_decide(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, external_id="x1", description="", language="fr")
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: _LONG_REAL_JD)
    pipeline.enrich_one(jid)
    assert _language_of(jid) == "fr"


def test_backfill_languages_sets_missing_and_overrides_wrong_labels(tmp_db, config):
    with db.connect() as conn:
        a = _insert(conn, config, external_id="a", title="Role A", description=_FR_JD)
        b = _insert(conn, config, external_id="b", title="Role B", description=_EN_JD, language="fr")
        c = _insert(conn, config, external_id="c", title="Role C", description="short", language="fr")
        d = _insert(conn, config, external_id="d", title="Role D", description=_EN_JD, language="en")
    summary = pipeline.backfill_languages()
    assert (_language_of(a), _language_of(b), _language_of(c), _language_of(d)) == ("fr", "en", "fr", "en")
    assert summary == {"checked": 4, "newly_set": 1, "label_overridden": 1}


def _drop_reasons():
    with db.connect() as conn:
        return [(r["reason"], r["company"]) for r in db.recent_fetch_drops(conn, hours=1)]


def test_judge_one_records_a_skip_for_short_descriptions(tmp_db, config):
    with db.connect() as conn:
        jid = _insert(conn, config, company="ShortCo", description="too short")
    assert pipeline.judge_one(jid).get("skipped")
    assert _drop_reasons() == [("judge_skipped_short", "ShortCo")]


@pytest.mark.parametrize("exc,reason", [
    (provider.LLMUnavailable("claude CLI failed (rc=1): usage limit"), "judge_llm_unavailable"),
    (subprocess.TimeoutExpired("claude", 180), "judge_timeout"),
    (json.JSONDecodeError("bad", "{", 0), "judge_bad_output"),
    (ValueError("no JSON object in LLM output"), "judge_bad_output"),
    (KeyError("x"), "judge_error"),
])
def test_judge_one_records_the_failure_reason_and_reraises(tmp_db, config, monkeypatch, exc, reason):
    with db.connect() as conn:
        jid = _insert(conn, config, company="FailCo", description=_REAL_JD)

    def failing_judge(job, preferences=""):
        raise exc

    monkeypatch.setattr(pipeline.llm_judge, "judge", failing_judge)
    with pytest.raises(type(exc)):
        pipeline.judge_one(jid)

    assert _drop_reasons() == [(reason, "FailCo")]
    with db.connect() as conn:
        assert db.get_job(conn, jid)["llm_score"] is None   # nothing stored: retried next run


def test_judge_all_excludes_short_description_jobs_from_the_queue(tmp_db, config, monkeypatch):
    """A short-description job must neither reach the LLM nor take a limit slot / log a
    skip row every run -- it waits until enrichment gives it real text."""
    with db.connect() as conn:
        short = _insert(conn, config, external_id="s", company="ShortCo",
                        description="machine learning deep learning nlp mlops pytorch")   # outscores below
        judgeable = _insert(conn, config, external_id="ok", company="OkCo", description=_REAL_JD)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 70, "verdict": "good", "seniority": "junior",
                         "min_years": 0, "reasons": "solid fit"})

    stats = pipeline.judge_all(min_score=0, limit=1)

    assert stats == {"candidates": 1, "judged": 1, "skipped_no_description": 0}
    with db.connect() as conn:
        assert db.get_job(conn, judgeable)["llm_score"] == 70
        assert db.get_job(conn, short)["llm_score"] is None
    assert _drop_reasons() == []


def test_daily_run_does_not_judge_jobs_whose_enriched_text_is_still_short(tmp_db, config, monkeypatch):
    fresh_job = Job(source="linkedin", external_id="98", title="Machine Learning Engineer",
                    company="Acme", location="Paris, Ile-de-France, France", url="http://x/98")
    monkeypatch.setattr(pipeline, "_gather", lambda cfg, force=False: [fresh_job])
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: "only a teaser")
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    called = []
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="": called.append(1))
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})

    summary = pipeline.daily_run(judge=True)

    assert summary["judged"] == 0 and called == []
    assert _drop_reasons() == []


def test_tailor_one_passes_the_language_through_and_stores_it_with_the_cv(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    seen = {}

    def fake(job, job_id, auto=False, judge_context=None, role_category="", language=None):
        seen["language"] = language
        return cv_engine.TailorResult(Path("/tmp/cv.tex"), Path("/tmp/cv.pdf"), "", "fr")
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake)

    result = pipeline.tailor_one(jid, language="fr")

    assert seen["language"] == "fr" and result["lang"] == "fr"
    with db.connect() as conn:
        artifact = db.list_cv_artifacts(conn, jid)[0]
    assert artifact["lang"] == "fr" and artifact["base_version"] == "cv_base_fr.tex"


def test_tailor_one_without_a_language_lets_the_engine_decide(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
    seen = {}

    def fake(job, job_id, auto=False, judge_context=None, role_category="", language=None):
        seen["language"] = language
        return cv_engine.TailorResult(Path("/tmp/cv.tex"), Path("/tmp/cv.pdf"))
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job", fake)

    pipeline.tailor_one(jid)

    assert seen["language"] is None
    with db.connect() as conn:
        artifact = db.list_cv_artifacts(conn, jid)[0]
    assert artifact["lang"] == "en" and artifact["base_version"] == "cv_base.tex"


def test_the_letter_follows_the_language_of_the_cv_it_accompanies(tmp_db, config, monkeypatch, tmp_path):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_EN_JD)          # an English posting...
        tex = tmp_path / "cv.tex"
        tex.write_text(r"\begin{document}CV\end{document}")
        db.add_cv_artifact(conn, jid, str(tex), str(tmp_path / "cv.pdf"), lang="fr")   # ...with a French CV
    seen = {}
    monkeypatch.setattr(pipeline.cover_letter, "draft_to_file",
                        lambda job, out_dir, judge_context=None, cv_text=None, language="en":
                        seen.update(language=language) or Path("/tmp/cover_letter.md"))
    result = pipeline.cover_one(jid)
    assert seen["language"] == "fr" and result["lang"] == "fr"


def test_the_letter_without_a_cv_follows_the_postings_language(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        # distinct titles/companies, or cross-source dedup folds the two into one row
        fr = _insert(conn, config, external_id="f", title="Ingénieur ML", company="FrCo", description=_FR_JD)
        en = _insert(conn, config, external_id="e", title="ML Engineer", company="EnCo", description=_EN_JD)
    seen = []
    monkeypatch.setattr(pipeline.cover_letter, "draft_to_file",
                        lambda job, out_dir, judge_context=None, cv_text=None, language="en":
                        seen.append(language) or Path("/tmp/cover_letter.md"))
    pipeline.cover_one(fr)
    pipeline.cover_one(en)
    assert seen == ["fr", "en"]


def test_check_duplicates_stops_calling_the_llm_once_the_session_limit_is_hit(tmp_db, config, monkeypatch):
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)
    monkeypatch.setattr(pipeline.dupes, "SAME_OVERLAP", 2.0)      # identical text still goes to the LLM
    with db.connect() as conn:
        _insert_dup_pair(conn, config)
        for ext, title in (("o-a", "ML Engineer (H/F)"), ("o-b", "ML Engineer")):
            _insert(conn, config, external_id=ext, company="OtherCo", title=title,
                    location="Paris, Ile-de-France, France", description=_LONG_REAL_JD)
    calls = []

    def fake_compare(job_a, job_b):
        calls.append(1)
        raise RuntimeError("claude CLI failed (rc=1): You've hit your session limit")

    monkeypatch.setattr(pipeline.llm_dedup, "compare", fake_compare)
    assert pipeline.check_duplicates(limit=10) == {"checked": 0, "same": 0, "filtered": 0, "by_rule": 0}
    assert len(calls) == 1
