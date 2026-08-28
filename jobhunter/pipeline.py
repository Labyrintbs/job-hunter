from __future__ import annotations

from . import db, match
from .config import load_search_config
from .sources import wttj
from .tailor import engine as cv_engine


def run_fetch(config: dict | None = None) -> dict:
    config = config or load_search_config()
    db.init_db()

    jobs = wttj.fetch(
        query=config["query"],
        max_hits=config.get("max_hits", 100),
        country=(config.get("countries") or ["France"])[0],
    )

    seen = 0
    kept = 0
    new = 0
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

    return {"fetched": seen, "kept": kept, "new": new}


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
