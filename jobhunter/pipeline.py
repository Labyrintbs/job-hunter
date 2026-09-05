from __future__ import annotations

import shutil
import time

from . import db, enrich, jd_store, match
from .apply import cover_letter
from .config import load_companies, load_search_config
from .llm import judge as llm_judge
from .llm import provider
from .notify import dispatch as notify_dispatch
from .sources import ats, francetravail, hellowork, linkedin, wttj
from .tailor import engine as cv_engine


def _fetch_wttj(config: dict) -> list:
    wt = config.get("wttj") or {}
    queries = wt.get("queries") or [config["query"]]
    jobs: list = []
    for q in queries:
        jobs += wttj.fetch(query=q, max_hits=config.get("max_hits", 100),
                            country=(config.get("countries") or ["France"])[0])
    return jobs


def _fetch_linkedin(config: dict) -> list:
    li = config.get("linkedin") or {}
    if not li.get("enabled"):
        return []
    return linkedin.fetch(
        queries=li.get("queries") or [config["query"]],
        locations=li.get("locations") or ["Paris, France"],
        max_pages=li.get("max_pages", 5),
        recent_hours=li.get("recent_hours", 168),
        max_retries=li.get("max_retries", 3),
        backoff_base=li.get("backoff_seconds", 2.0),
    )


def _fetch_francetravail(config: dict) -> list:
    ft = config.get("francetravail") or {}
    if not ft.get("enabled", True):
        return []
    departements = ft.get("departements", francetravail.IDF_DEPARTEMENTS)
    queries = ft.get("queries") or [config["query"]]
    jobs: list = []
    for q in queries:
        jobs += francetravail.fetch(query=q, departements=departements)
    return jobs


def _fetch_hellowork(config: dict) -> list:
    hw = config.get("hellowork") or {}
    if not hw.get("enabled"):
        return []
    queries = hw.get("queries") or [config["query"]]
    locations = hw.get("locations") or ["Paris"]
    jobs: list = []
    for query in queries:
        for location in locations:
            jobs += hellowork.fetch(query, location)
    return jobs


def _gather(config: dict) -> list:
    """Pull every enabled source. Each is isolated: one source's failure (a bad
    token, a network hiccup, a rate-limit) only drops that source's jobs, never
    the whole run."""
    sources = [
        ("wttj", lambda: _fetch_wttj(config)),
        ("ats", lambda: ats.fetch_all(load_companies())),
        ("linkedin", lambda: _fetch_linkedin(config)),
        ("francetravail", lambda: _fetch_francetravail(config)),
        ("hellowork", lambda: _fetch_hellowork(config)),
    ]
    jobs: list = []
    counts: dict[str, int] = {}
    for name, fn in sources:
        try:
            got = fn()
        except Exception as exc:
            print(f"  {name} warn: {exc}")
            got = []
        counts[name] = len(got)
        jobs += got
    print(f"  fetched by source: {counts}")
    return jobs


def run_fetch(config: dict | None = None, jobs: list | None = None) -> dict:
    config = config or load_search_config()
    db.init_db()

    if jobs is None:
        jobs = _gather(config)

    seen = 0
    kept = 0
    new_ids: list[int] = []
    filtered_new = 0
    per_source: dict[str, int] = {}
    # Geography of ALL new postings this run (filtered included) = the market signal.
    tier_new = {"idf": 0, "france": 0, "remote": 0, "outside": 0, "unknown": 0}
    with db.connect() as conn:
        config = {**config, "_active_rules": [dict(r) for r in db.active_rules(conn)]}
        for job in jobs:
            seen += 1
            s = match.screen(job, config)
            if not s.keep:
                continue
            kept += 1
            tier = match.geo_tier(job.location, config)
            jid, is_new = db.upsert_job(
                conn, job, s.score, s.reasons,
                filtered=s.filtered, filter_reason=s.filter_reason,
                seniority=s.seniority, min_years=s.min_years, geo_tier=tier,
                role_category=s.role_category,
            )
            if is_new:
                tier_new[tier] = tier_new.get(tier, 0) + 1
                for rid in s.matched_rules:
                    db.bump_rule_hits(conn, rid)
                if s.filtered:
                    filtered_new += 1
                else:
                    new_ids.append(jid)
                    per_source[job.source] = per_source.get(job.source, 0) + 1
                    # Pre-create the CV folder as soon as a job clears the filter (i.e.
                    # would show on the dashboard), so the JD (once fetched) and any
                    # tailored CV later land in the same place for offline analysis.
                    out_dir = cv_engine.CV_OUT_DIR / f"{jid}-{cv_engine._slug(job.company)}"
                    out_dir.mkdir(parents=True, exist_ok=True)

        stats = {
            "fetched": seen, "kept": kept, "new": len(new_ids),
            "filtered_new": filtered_new, "new_ids": new_ids, "new_by_source": per_source,
            "new_idf": tier_new["idf"], "new_france": tier_new["france"],
            "new_remote": tier_new["remote"], "new_outside": tier_new["outside"],
        }
        db.add_fetch_run(conn, stats)
    return stats


def _auto_tailor_jobs(job_ids: list[int], limit: int) -> int:
    """Tailor a CV + draft a cover letter for up to `limit` of the given job ids,
    skipping any that already have an artifact (idempotency guard). Shared by
    daily_run's inline "new this run" gate and process_backlog's backlog-wide
    sweep. Returns the count that actually compiled."""
    tailored = 0
    for jid in job_ids[:limit]:
        try:
            with db.connect() as conn:
                already = db.list_cv_artifacts(conn, jid)
            if already:
                continue
            print(f"  tailoring #{jid}...")
            result = tailor_one(jid, auto=True)
            cover_one(jid)
            if result.get("compiled"):
                tailored += 1
                print(f"  tailored #{jid}: compiled + cover letter drafted")
            else:
                print(f"  tailored #{jid}: compile failed (see cv.compile.log)")
        except Exception as exc:
            print(f"  auto-tailor warn: job {jid} failed: {exc}")
    return tailored


def daily_run(judge: bool = True, judge_min_score: int = 30, judge_limit: int = 15,
              auto_tailor: bool = True, auto_tailor_limit: int = 10) -> dict:
    """One scheduled run: fetch everywhere, enrich every new job with real JD content
    (LinkedIn/SmartRecruiters cards carry none up front), re-score with that content,
    THEN LLM-judge the new promising jobs (highest rule-score first, capped to bound
    cost) so the judge sees real descriptions instead of title-only stubs. Jobs the
    judge rates strong/good/stretch (i.e. not an outright "weak" fit) then get a CV
    auto-tailored + a cover letter drafted (capped separately, since each cover letter
    is its own LLM call) so they're ready for you to review and submit yourself --
    never auto-submitted. Returns a summary including the new job rows (for
    notification)."""
    config = load_search_config()
    stats = run_fetch(config)

    new_enriched = enrich_new(stats["new_ids"])["enriched"]
    engaged_enriched = enrich_pending(limit=10)["enriched"]

    judged = 0
    qualified: list[int] = []
    if judge and provider.available():
        new_set = set(stats["new_ids"])
        with db.connect() as conn:
            to_judge = [
                r["id"] for r in db.list_jobs(conn, min_score=judge_min_score)
                if r["id"] in new_set and r["llm_score"] is None
            ][:judge_limit]
        for jid in to_judge:
            try:
                result = judge_one(jid)
                judged += 1
                if result.get("skipped"):
                    print(f"  judged #{jid}: skipped ({result['skipped']})")
                else:
                    print(f"  judged #{jid}: {result['verdict']} ({result['score']})")
                if result.get("verdict") in ("strong", "good", "stretch"):
                    qualified.append(jid)
            except Exception as exc:
                print(f"  judge warn: job {jid} failed: {exc}")

    tailored = _auto_tailor_jobs(qualified, auto_tailor_limit) if auto_tailor else 0

    with db.connect() as conn:
        new_rows = [dict(db.get_job(conn, jid)) for jid in stats["new_ids"]]

    notified = notify_dispatch.send(new_rows, config)
    return {**stats, "judged": judged, "tailored": tailored,
            "enriched": new_enriched + engaged_enriched,
            "new_rows": new_rows, "notified": notified}


def process_backlog(judge_min_score: int = 30, judge_limit: int = 10,
                     tailor_limit: int = 10) -> dict:
    """Judge + auto-tailor cycle, decoupled from fetch cadence: sweeps the whole
    backlog (every not-yet-judged job, every judged-but-not-yet-tailored job)
    rather than only the jobs a single run just fetched. Meant to run on its own,
    more frequent cron schedule than the fetch cron -- fetching is the proven,
    LLM-free part of this pipeline; judging/tailoring are the parts that can hit
    an LLM quota, so keeping them on a separate, independently throttleable cron
    means a quota problem only ever stalls this side, never fetching itself."""
    db.init_db()
    if not provider.available():
        return {"enriched": 0, "judged": 0, "skipped_no_description": 0, "tailored": 0}

    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_pending_enrichment_any(conn, judge_limit)]
    enriched = 0
    for r in pending:
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")

    judge_stats = judge_all(min_score=judge_min_score, limit=judge_limit)

    with db.connect() as conn:
        candidates = [r["id"] for r in db.jobs_ready_for_auto_tailor(conn, tailor_limit)]
    tailored = _auto_tailor_jobs(candidates, tailor_limit)

    return {"enriched": enriched, "judged": judge_stats["judged"],
            "skipped_no_description": judge_stats["skipped_no_description"],
            "tailored": tailored}


def enrich_one(job_id: int) -> dict:
    """Fetch the full description for one job, store it, and re-score with that content
    (title-only scoring becomes content-aware once the JD lands -- this can also move a
    job into/out of the Filtered bucket, e.g. a '5+ years' requirement only visible in
    the body)."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        source, ext, url = row["source"], row["external_id"], row["url"]
        title, company = row["title"], row["company"]
    text = enrich.fetch_full_text(source, ext, url)
    if not text:
        return {"job_id": job_id, "enriched": False}
    jd_path = jd_store.save_jd(source=source, external_id=ext, title=title, company=company,
                                url=url, description=text)
    config = load_search_config()
    with db.connect() as conn:
        db.set_description(conn, job_id, text)
        job = db.job_from_row(db.get_job(conn, job_id))
        cfg = {**config, "_active_rules": [dict(r) for r in db.active_rules(conn)]}
        s = match.screen(job, cfg)
        db.update_screening(conn, job_id, s.score, s.reasons, filtered=s.filtered,
                            filter_reason=s.filter_reason, seniority=s.seniority,
                            min_years=s.min_years, role_category=s.role_category)
    if not s.filtered:
        # Keep a copy of the JD right next to where the tailored CV will land,
        # so both are in one place for later analysis.
        out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(company)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(jd_path, out_dir / "jd.txt")
    return {"job_id": job_id, "enriched": True, "chars": len(text),
            "score": s.score, "filtered": s.filtered}


def enrich_pending(limit: int = 20) -> dict:
    """Enrich engaged jobs (interested / past 'new') that lack a full description."""
    db.init_db()
    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_needing_enrichment(conn, limit)]
    enriched = 0
    for r in pending:
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")
    return {"candidates": len(pending), "enriched": enriched}


def enrich_new(job_ids: list[int]) -> dict:
    """Enrich every job from this run's fresh crop that still lacks a real description
    (LinkedIn guest cards and SmartRecruiters give none up front; WTTJ's profile field
    is sometimes empty). Unlike enrich_pending this isn't gated on engagement -- every
    new posting gets its full JD saved locally, which is what backs the rule-score
    content signal, the LLM judge, and any downstream corpus (e.g. embeddings) built
    from the DB. Throttled since it now includes LinkedIn's guest endpoint
    unconditionally rather than only for jobs you've engaged with."""
    db.init_db()
    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_by_id_needing_enrichment(conn, job_ids)]
    enriched = 0
    for i, r in enumerate(pending):
        if i:
            time.sleep(1.0)
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")
    return {"candidates": len(pending), "enriched": enriched}


# Below this, there's not enough real JD text to judge on -- title/company alone
# (LinkedIn guest cards, thin WTTJ profiles) makes the LLM guess rather than assess,
# and that guess tends to read as more confident/optimistic than it should.
_MIN_DESCRIPTION_CHARS = 100


def judge_one(job_id: int) -> dict:
    """LLM fit-judge one job; store score/verdict/reasons on the job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
        profile = db.current_profile(conn)
    if len((job.description or "").strip()) < _MIN_DESCRIPTION_CHARS:
        return {"job_id": job_id, "skipped": "no real JD content yet"}
    result = llm_judge.judge(job, preferences=profile["text"] if profile else "")
    with db.connect() as conn:
        db.set_llm_judgment(conn, job_id, result["score"], result["verdict"], result["reasons"])
        if result.get("seniority") or result.get("min_years") is not None:
            db.set_seniority(conn, job_id, result.get("seniority", ""), result.get("min_years"))
        if result["verdict"] == "weak":
            # A weak verdict is a more informed signal than the rule score that got it
            # onto the board in the first place -- auto-hide it like any other filter.
            db.set_llm_filter(conn, job_id, "llm judge: weak fit")
    return {"job_id": job_id, **result}


def judge_all(min_score: int = 40, limit: int | None = None) -> dict:
    """Judge every stored job at/above a rule-score threshold that isn't judged yet.
    Skips jobs with no real JD content rather than burning a call on a title-only guess --
    see judge_one's _MIN_DESCRIPTION_CHARS gate."""
    db.init_db()
    with db.connect() as conn:
        rows = [r for r in db.list_jobs(conn, min_score=min_score) if r["llm_score"] is None]
    if limit:
        rows = rows[:limit]
    judged = 0
    skipped = 0
    for r in rows:
        try:
            result = judge_one(r["id"])
            if result.get("skipped"):
                skipped += 1
                print(f"  judged #{r['id']}: skipped ({result['skipped']})")
            else:
                judged += 1
                print(f"  judged #{r['id']}: {result['verdict']} ({result['score']})")
        except Exception as exc:
            print(f"  judge warn: job {r['id']} failed: {exc}")
    return {"candidates": len(rows), "judged": judged, "skipped_no_description": skipped}


def cover_one(job_id: int) -> dict:
    """Draft a cover letter for one job; store the file path on the application."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
    out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(job.company)}"
    path = cover_letter.draft_to_file(job, out_dir)
    with db.connect() as conn:
        db.set_cover_letter(conn, job_id, str(path))
    return {"job_id": job_id, "cover_letter": str(path)}


def tailor_one(job_id: int, auto: bool = False) -> dict:
    """Tailor + compile a CV for one job, record it, and mark the job cv_ready.
    `auto=True` (daily_run's unsupervised path) also enforces the exact-2-page
    rule -- see cv_engine.tailor_job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)

    tex_path, pdf_path = cv_engine.tailor_job(job, job_id, auto=auto)

    with db.connect() as conn:
        db.add_cv_artifact(conn, job_id, str(tex_path), str(pdf_path or ""),
                           base_version="cv_base.tex", origin="ai")
        if pdf_path:
            db.update_status(conn, job_id, "cv_ready")
    return {
        "job_id": job_id,
        "tex": str(tex_path),
        "pdf": str(pdf_path) if pdf_path else None,
        "compiled": pdf_path is not None,
    }


def import_revised_cv(job_id: int, pdf: "Path | bytes", tex: "Path | None" = None) -> dict:
    """Store a human-revised CV against a job. It becomes the active CV (latest-wins)
    without touching the AI versions, which stay on disk. `pdf` is a path or raw bytes."""
    from pathlib import Path
    import shutil
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        company = row["company"]

    out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(company)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    pdf_dest = out_dir / f"revised-{stamp}.pdf"
    if isinstance(pdf, (bytes, bytearray)):
        pdf_dest.write_bytes(pdf)
    else:
        shutil.copy(Path(pdf), pdf_dest)

    tex_dest = ""
    if tex is not None:
        tex_dest_path = out_dir / f"revised-{stamp}.tex"
        shutil.copy(Path(tex), tex_dest_path)
        tex_dest = str(tex_dest_path)

    with db.connect() as conn:
        db.add_cv_artifact(conn, job_id, tex_dest, str(pdf_dest),
                           base_version="revised", origin="revised")
        db.update_status(conn, job_id, "cv_ready")
    return {"job_id": job_id, "pdf": str(pdf_dest), "tex": tex_dest or None, "origin": "revised"}


def _timestamp() -> str:
    """A filesystem-safe timestamp. Isolated so tests can monkeypatch it deterministically."""
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")
