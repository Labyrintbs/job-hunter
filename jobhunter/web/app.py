from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..llm import provider
from ..pipeline import cover_one, judge_one, run_fetch, tailor_one

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Job Hunter")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str | None = None, min_score: int = 0, filtered: int = 0):
    with db.connect() as conn:
        jobs = db.list_jobs(conn, status=status or None, min_score=min_score, filtered=filtered)
        counts = db.status_counts(conn)
        n_filtered = db.filtered_count(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs,
            "counts": counts,
            "statuses": db.STATUSES,
            "active_status": status or "",
            "min_score": min_score,
            "filtered": filtered,
            "n_filtered": n_filtered,
            "total": sum(counts.values()),
            "llm_available": provider.available(),
        },
    )


@app.get("/kanban", response_class=HTMLResponse)
def kanban(request: Request):
    with db.connect() as conn:
        by_status = {s: db.list_jobs(conn, status=s) for s in db.STATUSES}
    return TEMPLATES.TemplateResponse(
        request,
        "kanban.html",
        {"statuses": db.STATUSES, "by_status": by_status},
    )


@app.post("/status")
def set_status(job_id: int = Form(...), status: str = Form(...)):
    with db.connect() as conn:
        db.update_status(conn, job_id, status)
    return JSONResponse({"ok": True, "job_id": job_id, "status": status})


@app.post("/filter/restore/{job_id}")
def restore_route(job_id: int):
    """Move a job out of the auto-hidden Filtered bucket back into the main list."""
    with db.connect() as conn:
        db.set_filtered(conn, job_id, False, "")
    return JSONResponse({"ok": True, "job_id": job_id})


@app.post("/run/fetch")
def run_fetch_route():
    run_fetch()
    return RedirectResponse(url="/", status_code=303)


@app.post("/tailor/{job_id}")
def tailor_route(job_id: int):
    return JSONResponse(tailor_one(job_id))


@app.post("/judge/{job_id}")
def judge_route(job_id: int):
    if not provider.available():
        return JSONResponse({"error": "no LLM backend"}, status_code=503)
    return JSONResponse(judge_one(job_id))


@app.post("/cover/{job_id}")
def cover_route(job_id: int):
    if not provider.available():
        return JSONResponse({"error": "no LLM backend"}, status_code=503)
    return JSONResponse(cover_one(job_id))
