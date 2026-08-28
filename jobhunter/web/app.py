from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..pipeline import run_fetch

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Job Hunter")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str | None = None, min_score: int = 0):
    with db.connect() as conn:
        jobs = db.list_jobs(conn, status=status or None, min_score=min_score)
        counts = db.status_counts(conn)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs,
            "counts": counts,
            "statuses": db.STATUSES,
            "active_status": status or "",
            "min_score": min_score,
            "total": sum(counts.values()),
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


@app.post("/run/fetch")
def run_fetch_route():
    stats = run_fetch()
    return RedirectResponse(url="/", status_code=303)
