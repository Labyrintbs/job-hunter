from __future__ import annotations

from . import db, enrich, match
from .apply import cover_letter
from .config import load_companies, load_search_config
from .llm import judge as llm_judge
from .llm import provider
from .notify import dispatch as notify_dispatch
from .sources import ats, linkedin, wttj
from .tailor import engine as cv_engine


def _gather(config: dict) -> list:
    jobs = wttj.fetch(
        query=config["query"],
        max_hits=config.get("max_hits", 100),
        country=(config.get("countries") or ["France"])[0],
    )
    jobs += ats.fetch_all(load_companies())

    li = config.get("linkedin") or {}
    if li.get("enabled"):
        try:
            jobs += linkedin.fetch(
                query=config["query"],
                location=li.get("location", "Paris, France"),
                max_pages=li.get("max_pages", 3),
                recent_hours=li.get("recent_hours", 168),
            )
        except Exception as exc:
            print(f"  linkedin warn: {exc}")
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

        stats = {
            "fetched": seen, "kept": kept, "new": len(new_ids),
            "filtered_new": filtered_new, "new_ids": new_ids, "new_by_source": per_source,
            "new_idf": tier_new["idf"], "new_france": tier_new["france"],
            "new_remote": tier_new["remote"], "new_outside": tier_new["outside"],
        }
        db.add_fetch_run(conn, stats)
    return stats


def daily_run(judge: bool = True, judge_min_score: int = 40, judge_limit: int = 15) -> dict:
    """One scheduled run: fetch everywhere, then LLM-judge the new promising jobs
    (highest rule-score first, capped to bound cost). Returns a summary including
    the new job rows (for notification)."""
    config = load_search_config()
    stats = run_fetch(config)

    judged = 0
    if judge and provider.available():
        new_set = set(stats["new_ids"])
        with db.connect() as conn:
            to_judge = [
                r["id"] for r in db.list_jobs(conn, min_score=judge_min_score)
                if r["id"] in new_set and r["llm_score"] is None
            ][:judge_limit]
        for jid in to_judge:
            try:
                judge_one(jid)
                judged += 1
            except Exception as exc:
                print(f"  judge warn: job {jid} failed: {exc}")

    enriched = enrich_pending(limit=10)["enriched"]

    with db.connect() as conn:
        new_rows = [dict(db.get_job(conn, jid)) for jid in stats["new_ids"]]

    notified = notify_dispatch.send(new_rows, config)
    return {**stats, "judged": judged, "enriched": enriched,
            "new_rows": new_rows, "notified": notified}


def enrich_one(job_id: int) -> dict:
    """Fetch the full description for one job and store it."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        source, ext, url = row["source"], row["external_id"], row["url"]
    text = enrich.fetch_full_text(source, ext, url)
    if not text:
        return {"job_id": job_id, "enriched": False}
    with db.connect() as conn:
        db.set_description(conn, job_id, text)
    return {"job_id": job_id, "enriched": True, "chars": len(text)}


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


def judge_one(job_id: int) -> dict:
    """LLM fit-judge one job; store score/verdict/reasons on the job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
        profile = db.current_profile(conn)
    result = llm_judge.judge(job, preferences=profile["text"] if profile else "")
    with db.connect() as conn:
        db.set_llm_judgment(conn, job_id, result["score"], result["verdict"], result["reasons"])
        if result.get("seniority") or result.get("min_years") is not None:
            db.set_seniority(conn, job_id, result.get("seniority", ""), result.get("min_years"))
    return {"job_id": job_id, **result}


def judge_all(min_score: int = 40, limit: int | None = None) -> dict:
    """Judge every stored job at/above a rule-score threshold that isn't judged yet."""
    db.init_db()
    with db.connect() as conn:
        rows = [r for r in db.list_jobs(conn, min_score=min_score) if r["llm_score"] is None]
    if limit:
        rows = rows[:limit]
    judged = 0
    for r in rows:
        try:
            judge_one(r["id"])
            judged += 1
        except Exception as exc:
            print(f"  judge warn: job {r['id']} failed: {exc}")
    return {"candidates": len(rows), "judged": judged}


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


def tailor_one(job_id: int) -> dict:
    """Tailor + compile a CV for one job, record it, and mark the job cv_ready."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)

    tex_path, pdf_path = cv_engine.tailor_job(job, job_id)

    with db.connect() as conn:
        db.add_cv_artifact(conn, job_id, str(tex_path), str(pdf_path or ""), base_version="cv_base.tex")
        if pdf_path:
            db.update_status(conn, job_id, "cv_ready")
    return {
        "job_id": job_id,
        "tex": str(tex_path),
        "pdf": str(pdf_path) if pdf_path else None,
        "compiled": pdf_path is not None,
    }
