from jobhunter import db, pipeline
from jobhunter.models import Job
from jobhunter.tailor import engine as cv_engine


def test_gather_survives_a_source_exception(config, monkeypatch):
    """A WTTJ (or any source) failure must not crash the whole run -- previously
    only ats/linkedin were wrapped in try/except; wttj wasn't."""
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies: [])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: [])

    jobs = pipeline._gather(config)   # must not raise
    assert jobs == []


def test_gather_collects_every_source(config, monkeypatch):
    make = lambda src, i: Job(source=src, external_id=str(i), title="ML Engineer", company="Acme")
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: [make("wttj", 1)])
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies: [make("ats", 2)])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [make("linkedin", 3)])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [make("francetravail", 4)])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: [make("hellowork", 5)])

    jobs = pipeline._gather(config)
    assert {j.source for j in jobs} == {"wttj", "ats", "linkedin", "francetravail", "hellowork"}


def test_fetch_wttj_loops_over_configured_queries(monkeypatch):
    calls = []

    def fake_fetch(query, max_hits, country):
        calls.append(query)
        return [Job(source="wttj", external_id=query, title=query, company="Acme")]

    monkeypatch.setattr(pipeline.wttj, "fetch", fake_fetch)
    cfg = {"query": "machine learning engineer", "max_hits": 100, "countries": ["France"],
           "wttj": {"queries": ["machine learning engineer", "nlp engineer"]}}
    jobs = pipeline._fetch_wttj(cfg)
    assert calls == ["machine learning engineer", "nlp engineer"]
    assert len(jobs) == 2


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


def test_run_fetch_persists_role_category(tmp_db, config, monkeypatch):
    cv_job = Job(source="wttj", external_id="42", title="Computer Vision Engineer",
                company="Acme", location="Paris, Ile-de-France, France")
    monkeypatch.setattr(pipeline, "_gather", lambda cfg: [cv_job])
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
    monkeypatch.setattr(pipeline, "_gather", lambda cfg: [kept, filtered])
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
    monkeypatch.setattr(pipeline, "_gather", lambda cfg: [fresh_job])

    marker = "SPECIAL_MARKER_ONLY_PRESENT_AFTER_ENRICHMENT"
    monkeypatch.setattr(pipeline.enrich, "fetch_full_text", lambda *a, **k: marker)
    monkeypatch.setattr(pipeline.provider, "available", lambda: True)

    captured = {}

    def fake_judge(job, preferences=""):
        captured["description"] = job.description
        return {"score": 80, "verdict": "good", "seniority": "junior",
                "min_years": 1, "reasons": "fit"}

    monkeypatch.setattr(pipeline.llm_judge, "judge", fake_judge)
    monkeypatch.setattr(pipeline.notify_dispatch, "send", lambda rows, cfg: {"selected": 0, "results": {}})

    summary = pipeline.daily_run(judge=True)

    assert summary["judged"] == 1
    assert captured["description"] == marker   # judge saw the enriched content, not empty/title-only


def test_judge_one_auto_hides_weak_verdict(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 15, "verdict": "weak", "seniority": "junior",
                         "min_years": 0, "reasons": "domain mismatch"})

    pipeline.judge_one(jid)

    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["llm_score"] == 15
    assert row["filtered"] == 1
    assert row["filter_reason"] == "llm judge: weak fit"


def test_judge_one_does_not_hide_good_or_stretch_verdicts(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config)
    monkeypatch.setattr(pipeline.llm_judge, "judge", lambda job, preferences="":
                        {"score": 55, "verdict": "stretch", "seniority": "junior",
                         "min_years": 0, "reasons": "uncertain fit"})

    pipeline.judge_one(jid)

    with db.connect() as conn:
        row = db.get_job(conn, jid)
    assert row["llm_score"] == 55
    assert row["filtered"] == 0
