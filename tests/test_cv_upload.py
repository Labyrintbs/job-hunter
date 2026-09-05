from pathlib import Path

from jobhunter import db, pipeline
from jobhunter.models import Job
from jobhunter.tailor import engine as cv_engine


def _seed(conn):
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="Acme", location="Paris")
    return db.upsert_job(conn, job, 60, "r")[0]


def test_import_revised_becomes_active(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cv_engine, "CV_OUT_DIR", tmp_path / "cv")
    monkeypatch.setattr(pipeline, "_timestamp", lambda: "20260101-000000")
    with db.connect() as conn:
        jid = _seed(conn)

    res = pipeline.import_revised_cv(jid, b"%PDF-1.4 fake")
    assert res["origin"] == "revised"
    assert res["pdf"].endswith("revised-20260101-000000.pdf")
    assert Path(res["pdf"]).read_bytes().startswith(b"%PDF")

    with db.connect() as conn:
        row = db.get_job(conn, jid)
        assert row["status"] == "cv_ready"
        listed = db.list_jobs(conn)[0]
        assert listed["cv_pdf"] == res["pdf"]
        assert listed["cv_origin"] == "revised"
        assert listed["cv_versions"] == 1


def test_revised_preserved_when_ai_added_later(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cv_engine, "CV_OUT_DIR", tmp_path / "cv")
    monkeypatch.setattr(pipeline, "_timestamp", lambda: "20260101-000000")
    with db.connect() as conn:
        jid = _seed(conn)
    revised_pdf = Path(pipeline.import_revised_cv(jid, b"%PDF revised")["pdf"])

    ai_pdf = revised_pdf.parent / "cv.pdf"        # AI tailor uses a distinct fixed name
    ai_pdf.write_bytes(b"%PDF ai")
    with db.connect() as conn:
        db.add_cv_artifact(conn, jid, str(revised_pdf.parent / "cv.tex"), str(ai_pdf), origin="ai")
        arts = db.list_cv_artifacts(conn, jid)
        assert arts[0]["origin"] == "ai"                       # newest wins
        assert {a["origin"] for a in arts} == {"ai", "revised"}
    assert revised_pdf.exists() and revised_pdf.read_bytes() == b"%PDF revised"   # not clobbered


def test_import_with_tex(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cv_engine, "CV_OUT_DIR", tmp_path / "cv")
    monkeypatch.setattr(pipeline, "_timestamp", lambda: "t")
    src_tex = tmp_path / "my.tex"
    src_tex.write_text("\\documentclass{article}")
    with db.connect() as conn:
        jid = _seed(conn)
    res = pipeline.import_revised_cv(jid, b"%PDF", tex=src_tex)
    assert res["tex"] and Path(res["tex"]).exists()


def test_import_missing_job(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(cv_engine, "CV_OUT_DIR", tmp_path / "cv")
    assert pipeline.import_revised_cv(9999, b"%PDF").get("error") == "not found"


def test_failed_compile_shows_attempted_but_no_pdf(tmp_db):
    """tailor_one always records an artifact even when the PDF fails to compile
    (pdf_path=""), so the dashboard can tell "tried and failed" apart from
    "never attempted" using cv_versions > 0 with cv_pdf falsy -- no new column."""
    with db.connect() as conn:
        jid = _seed(conn)
        db.add_cv_artifact(conn, jid, "/tmp/cv.tex", "", origin="ai")
        listed = db.list_jobs(conn)[0]
    assert listed["cv_versions"] == 1
    assert not listed["cv_pdf"]
