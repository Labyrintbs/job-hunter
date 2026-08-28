"""Tailor the base CV to a job and compile it to PDF."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import DATA_DIR, REPO_ROOT
from ..models import Job
from . import snippet_bank
from .snippet_bank import Block, ParsedCV

BASE_CV = REPO_ROOT / "templates" / "cv_base.tex"
CV_OUT_DIR = DATA_DIR / "cv"


def _job_terms(job: Job) -> set[str]:
    return snippet_bank.terms_in(f"{job.title} {job.description}")


def _rank(blocks: list[Block], terms: set[str]) -> list[Block]:
    return sorted(blocks, key=lambda b: len(b.tags & terms), reverse=True)


_TEX_ESCAPES = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


def _tex_escape(text: str) -> str:
    return "".join(_TEX_ESCAPES.get(ch, ch) for ch in text)


def _tagline(job: Job) -> str:
    role = _tex_escape(job.title.strip().rstrip("."))
    company = _tex_escape(job.company.strip())
    who = f" at {company}" if company else ""
    return (
        f"{{Seeking a full-time Machine Learning role (CDI) — targeting "
        f"{role}{who}; Île-de-France, open to mobility}}"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def tailor_tex(job: Job, parsed: ParsedCV | None = None) -> str:
    parsed = parsed or snippet_bank.parse(BASE_CV)
    terms = _job_terms(job)
    doc = parsed.document

    doc = snippet_bank.reassemble(doc, r"PROJECTS[^}]*", _rank(parsed.projects, terms))
    doc = snippet_bank.reassemble(doc, r"PROFESSIONAL EXPERIENCE", _rank(parsed.experiences, terms))
    if parsed.heading_line:
        doc = doc.replace(parsed.heading_line, _tagline(job), 1)
    return doc


def compile_tex(tex: str, out_dir: Path, name: str = "cv") -> Path | None:
    """Compile tex to PDF via latexmk. Returns the PDF path, or None on failure
    (the .tex is still written for manual fixing)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{name}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    glyph = REPO_ROOT / "templates" / "glyphtounicode.tex"  # optional; latex has it built-in
    with tempfile.TemporaryDirectory() as tmp:
        tmp_tex = Path(tmp) / f"{name}.tex"
        tmp_tex.write_text(tex, encoding="utf-8")
        proc = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", tmp_tex.name],
            cwd=tmp, capture_output=True, text=True,
        )
        tmp_pdf = Path(tmp) / f"{name}.pdf"
        if proc.returncode != 0 or not tmp_pdf.exists():
            (out_dir / f"{name}.compile.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            return None
        pdf_path = out_dir / f"{name}.pdf"
        shutil.copy(tmp_pdf, pdf_path)
        return pdf_path


def tailor_job(job: Job, job_id: int) -> tuple[Path, Path | None]:
    """Generate + compile a tailored CV for a job. Returns (tex_path, pdf_path)."""
    tex = tailor_tex(job)
    out_dir = CV_OUT_DIR / f"{job_id}-{_slug(job.company)}"
    pdf = compile_tex(tex, out_dir, name="cv")
    return out_dir / "cv.tex", pdf
