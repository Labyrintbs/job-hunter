from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, export as export_mod, learn
from ..llm import provider
from ..pipeline import cover_one, enrich_one, import_revised_cv, judge_one, run_fetch, tailor_one

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Job Hunter")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str | None = None, min_score: int = 0,
              filtered: int = 0, dismissed: int = 0):
    with db.connect() as conn:
        if dismissed:
            jobs = db.list_jobs(conn, status=status or None, min_score=min_score,
                                filtered=None, dismissed=True)
        else:
            jobs = db.list_jobs(conn, status=status or None, min_score=min_score,
                                filtered=filtered, dismissed=False)
        counts = db.status_counts(conn)
        n_filtered = db.filtered_count(conn)
        n_dismissed = db.dismissed_count(conn)
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
            "dismissed": dismissed,
            "n_filtered": n_filtered,
            "n_dismissed": n_dismissed,
            "dismiss_reasons": db.DISMISS_REASONS,
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


@app.post("/feedback/{job_id}")
def feedback_route(job_id: int, label: str = Form(...), reasons: str = Form("")):
    """Explicit judgment: 'interested' (also rescues from Filtered), 'dismissed'
    (with reason chips), or '' to clear."""
    try:
        with db.connect() as conn:
            db.set_feedback(conn, job_id, label, reasons)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "job_id": job_id, "label": label})


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


@app.post("/enrich/{job_id}")
def enrich_route(job_id: int):
    return JSONResponse(enrich_one(job_id))


@app.post("/cv/upload/{job_id}")
async def cv_upload_route(job_id: int, pdf: UploadFile = File(...)):
    """Store a human-revised CV (PDF) as the job's active CV."""
    data = await pdf.read()
    if not data:
        return JSONResponse({"error": "empty file"}, status_code=400)
    result = import_revised_cv(job_id, data)
    status = 404 if result.get("error") else 200
    return JSONResponse(result, status_code=status)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    with db.connect() as conn:
        pending = db.list_rules(conn, active=0)
        active = db.list_rules(conn, active=1)
        n_dismissed = db.dismissed_count(conn)
        profile = db.current_profile(conn)
        metrics = db.false_negative_stats(conn)
    return TEMPLATES.TemplateResponse(
        request, "rules.html",
        {"pending": pending, "active": active, "n_dismissed": n_dismissed,
         "profile": profile, "metrics": metrics, "llm_available": provider.available()},
    )


@app.post("/rules/mine")
def rules_mine_route():
    with db.connect() as conn:
        learn.mine_rules(conn)
    return RedirectResponse(url="/rules", status_code=303)


@app.post("/rules/{rule_id}/approve")
def rules_approve_route(rule_id: int):
    with db.connect() as conn:
        db.set_rule_active(conn, rule_id, 1)
    return JSONResponse({"ok": True, "rule_id": rule_id})


@app.post("/rules/{rule_id}/reject")
def rules_reject_route(rule_id: int):
    with db.connect() as conn:
        db.delete_rule(conn, rule_id)
    return JSONResponse({"ok": True, "rule_id": rule_id})


@app.get("/export/{view}.csv")
def export_csv_route(view: str):
    """CSV for a single analytics view (for a Grafana Infinity/CSV datasource)."""
    try:
        with db.connect() as conn:
            rows = export_mod.view_rows(conn, view)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(export_mod.to_csv(rows), media_type="text/csv")


@app.post("/profile/update")
def profile_update_route():
    if not provider.available():
        return RedirectResponse(url="/rules", status_code=303)
    with db.connect() as conn:
        learn.condense_profile(conn)
    return RedirectResponse(url="/rules", status_code=303)
