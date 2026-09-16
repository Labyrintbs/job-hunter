from pathlib import Path

from jobhunter import db, pipeline
from jobhunter.models import Job
from jobhunter.tailor import engine as cv_engine


def test_gather_survives_a_source_exception(tmp_db, config, monkeypatch):
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


def test_gather_collects_every_source(tmp_db, config, monkeypatch):
    make = lambda src, i: Job(source=src, external_id=str(i), title="ML Engineer", company="Acme")
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: [make("wttj", 1)])
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies: [make("ats", 2)])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [make("linkedin", 3)])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [make("francetravail", 4)])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: [make("hellowork", 5)])

    jobs = pipeline._gather(config)
    assert {j.source for j in jobs} == {"wttj", "ats", "linkedin", "francetravail", "hellowork"}


def _stub_all_sources_except_hellowork(monkeypatch, called):
    monkeypatch.setattr(pipeline.wttj, "fetch", lambda **k: [])
    monkeypatch.setattr(pipeline.ats, "fetch_all", lambda companies: [])
    monkeypatch.setattr(pipeline, "load_companies", lambda: [])
    monkeypatch.setattr(pipeline, "_fetch_linkedin", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_francetravail", lambda cfg: [])
    monkeypatch.setattr(pipeline, "_fetch_hellowork", lambda cfg: called.append(1) or [])


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
    probed = []
    monkeypatch.setattr(pipeline.ats_discovery, "probe", lambda company, **kw:
                        probed.append(company) or "possible greenhouse board: token=x, 3 postings")

    pipeline.judge_one(jid)

    assert probed == ["Brand New Startup"]
    with db.connect() as conn:
        companies = {c["name"]: c["last_result"] for c in db.list_target_companies(conn)}
    assert companies["Brand New Startup"] == "possible greenhouse board: token=x, 3 postings"


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

    assert stats == {"checked": 0, "same": 0, "filtered": 0}
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
    assert stats == {"checked": 1, "same": 0, "filtered": 0}
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

    assert stats == {"checked": 1, "same": 1, "filtered": 1}
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

    assert stats == {"checked": 1, "same": 0, "filtered": 0}
    with db.connect() as conn:
        assert db.get_job(conn, a)["filtered"] == 0
        assert db.get_job(conn, b)["filtered"] == 0


def test_tailor_one_and_cover_one_pass_the_judge_context_through(tmp_db, config, monkeypatch):
    with db.connect() as conn:
        jid = _insert(conn, config, description=_REAL_JD)
        db.set_llm_judgment(conn, jid, 89, "strong", "great domain match")

    captured = {}
    monkeypatch.setattr(pipeline.cv_engine, "tailor_job",
                        lambda job, job_id, auto=False, judge_context=None, role_category="":
                        captured.update(tailor_ctx=judge_context) or (Path("/tmp/cv.tex"), Path("/tmp/cv.pdf")))
    monkeypatch.setattr(pipeline.cover_letter, "draft_to_file",
                        lambda job, out_dir, judge_context=None:
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
                        lambda job, job_id, auto=False, judge_context=None, role_category="":
                        captured.update(tailor_ctx=judge_context) or (Path("/tmp/cv.tex"), Path("/tmp/cv.pdf")))

    pipeline.tailor_one(jid)

    assert captured["tailor_ctx"] is None
