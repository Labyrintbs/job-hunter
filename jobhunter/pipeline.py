from __future__ import annotations

from . import db, match
from .apply import cover_letter
from .config import load_companies, load_search_config
from .llm import judge as llm_judge
from .llm import provider
from .sources import ats, wttj
from .tailor import engine as cv_engine


def _gather(config: dict) -> list:
    jobs = wttj.fetch(
        query=config["query"],
        max_hits=config.get("max_hits", 100),
        country=(config.get("countries") or ["France"])[0],
    )
    jobs += ats.fetch_all(load_companies())
    return jobs


def run_fetch(config: dict | None = None) -> dict:
    config = config or load_search_config()
    db.init_db()

    jobs = _gather(config)

    seen = 0
    kept = 0
    new = 0
    per_source: dict[str, int] = {}
    with db.connect() as conn:
        for job in jobs:
            seen += 1
            pts, reasons, keep = match.evaluate(job, config)
            if not keep:
                continue
            kept += 1
            _, is_new = db.upsert_job(conn, job, pts, reasons)
            if is_new:
                new += 1
                per_source[job.source] = per_source.get(job.source, 0) + 1

    return {"fetched": seen, "kept": kept, "new": new, "new_by_source": per_source}


def judge_one(job_id: int) -> dict:
    """LLM fit-judge one job; store score/verdict/reasons on the job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
    result = llm_judge.judge(job)
    with db.connect() as conn:
        db.set_llm_judgment(conn, job_id, result["score"], result["verdict"], result["reasons"])
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
