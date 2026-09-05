"""Tailor the base CV to a job and compile it to PDF."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import DATA_DIR, REPO_ROOT
from ..llm import provider
from ..models import Job
from . import select as llm_select
from . import snippet_bank
from .snippet_bank import Block, ParsedCV, SkillCategory

BASE_CV = REPO_ROOT / "templates" / "cv_base.tex"
CV_OUT_DIR = DATA_DIR / "cv"


def _job_terms(job: Job) -> set[str]:
    return snippet_bank.terms_in(f"{job.title} {job.description}")


# Caps mirror templates/cv_tailoring_workflow.md's Step 2 ("what to cut"): 2
# Professional Experience entries, 3 Projects & Research entries by default.
MAX_EXPERIENCES = 2
MAX_PROJECTS = 3

# Only ever dropped for a job with no vision/medical signal -- the one category
# every manually-tailored CV this session has actually dropped. See
# cv_tailoring_workflow.md's "Emphasis" step.
_CONDITIONAL_SKILL_CATEGORY = r"Computer Vision \& Medical Imaging"


def _fallback_select(blocks: list[Block], terms: set[str], cap: int) -> list[Block]:
    """Deterministic fallback when the LLM selection call is unavailable or
    returns something unusable: pick up to `cap` blocks by keyword-tag overlap
    (ties broken toward the more recent one), then always display the selection
    in reverse-chronological order. Never invents anything either way, it only
    picks from the real, existing blocks -- see _select_blocks for the primary,
    LLM-driven path."""
    if len(blocks) <= cap:
        chosen = list(blocks)
    else:
        ranked = sorted(blocks, key=lambda b: (len(b.tags & terms), b.end_date()), reverse=True)
        chosen = ranked[:cap]
    return sorted(chosen, key=lambda b: b.end_date(), reverse=True)


def _fallback_select_skills(categories: list[SkillCategory], terms: set[str]) -> list[SkillCategory]:
    """Deterministic fallback: only ever drops the one category that's actually
    been dropped in practice, and only when irrelevant."""
    return [c for c in categories if c.name != _CONDITIONAL_SKILL_CATEGORY or (c.tags & terms)]


def _apply_ids(blocks: list[Block], ids: object, cap: int) -> list[Block]:
    """Map the LLM's chosen ids back to real blocks, preserving its given order
    (it was told to default to reverse-chronological and only deviate with a
    stated reason -- trust that judgment rather than re-sorting here). Ignores
    out-of-range/duplicate/malformed ids rather than raising, since a slightly
    messy response shouldn't crash tailoring."""
    if not isinstance(ids, list):
        return []
    seen: set[int] = set()
    chosen = []
    for i in ids:
        if isinstance(i, int) and 0 <= i < len(blocks) and i not in seen:
            chosen.append(blocks[i])
            seen.add(i)
    return chosen[:cap]


def _apply_selection(blocks: list[Block], ids: object, bullets_per_id: object, cap: int) -> list[Block]:
    """Like _apply_ids, but also trims each chosen block to the bullets picked
    for it (bullets_per_id[i] corresponds to ids[i]) via
    snippet_bank.filter_bullets -- reuse-only, never invents a bullet, only
    ever drops from what's already there. A missing/malformed bullet list for
    a given entry just keeps that entry's bullets unfiltered."""
    if not isinstance(ids, list):
        return []
    bullets_per_id = bullets_per_id if isinstance(bullets_per_id, list) else []
    seen: set[int] = set()
    chosen: list[Block] = []
    for pos, i in enumerate(ids):
        if not (isinstance(i, int) and 0 <= i < len(blocks) and i not in seen):
            continue
        seen.add(i)
        block = blocks[i]
        keep = bullets_per_id[pos] if pos < len(bullets_per_id) and isinstance(bullets_per_id[pos], list) else None
        text = snippet_bank.filter_bullets(block.text, keep) if keep else block.text
        chosen.append(Block(text=text, tags=block.tags))
        if len(chosen) >= cap:
            break
    return chosen


def _apply_names(categories: list[SkillCategory], names: object) -> list[SkillCategory]:
    if not isinstance(names, list):
        return []
    by_name = {c.name: c for c in categories}
    return [by_name[n] for n in names if isinstance(n, str) and n in by_name]


def _menu_pairs(blocks: list[Block]) -> list[tuple[str, list[str]]]:
    return [(b.text, b.bullets()) for b in blocks]


def _select_blocks(job: Job, parsed: ParsedCV, terms: set[str], feedback: str | None = None
                    ) -> tuple[list[Block], list[Block], list[SkillCategory]]:
    """Decide which experiences/projects/skill categories (and which bullets
    within them) to keep, mirroring templates/cv_tailoring_workflow.md (same
    rules used tailoring by hand): an LLM call chooses from the real, existing
    content (see tailor/select.py), falling back to a deterministic
    keyword-overlap heuristic (whole blocks, no bullet trimming) if the LLM
    backend is unavailable or its response is unusable, so tailoring never
    hard-fails just because that call did. `feedback` (optional) is a hint from
    a previous compile attempt that didn't fit the page -- see tailor_job's
    retry. Returns (projects, experiences, skills)."""
    if provider.available():
        try:
            result = llm_select.select(
                job,
                _menu_pairs(parsed.experiences),
                _menu_pairs(parsed.projects),
                [c.name for c in parsed.skills],
                feedback=feedback,
            )
            experiences = _apply_selection(parsed.experiences, result.get("experience_ids"),
                                            result.get("experience_bullets"), MAX_EXPERIENCES)
            projects = _apply_selection(parsed.projects, result.get("project_ids"),
                                         result.get("project_bullets"), MAX_PROJECTS)
            skills = _apply_names(parsed.skills, result.get("skill_categories"))
            if experiences and projects and skills:
                return projects, experiences, skills
        except Exception:
            pass  # fall through to the deterministic path below

    return (_fallback_select(parsed.projects, terms, MAX_PROJECTS),
            _fallback_select(parsed.experiences, terms, MAX_EXPERIENCES),
            _fallback_select_skills(parsed.skills, terms))


# Update when the target start date changes. Kept as one constant so it's
# never silently dropped by a re-tailor (see templates/cv_tailoring_workflow.md).
AVAILABILITY = "September 2026"


def _tagline() -> str:
    # Deliberately generic, same line as templates/cv_base.tex, no per-job
    # "targeting <role> at <company>" clause. See templates/cv_tailoring_workflow.md.
    return (
        f"{{Seeking a full-time Machine Learning role (CDI) from {AVAILABILITY} — "
        f"Île-de-France, open to mobility}}"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def tailor_tex(job: Job, parsed: ParsedCV | None = None, feedback: str | None = None) -> str:
    parsed = parsed or snippet_bank.parse(BASE_CV)
    terms = _job_terms(job)
    doc = parsed.document

    projects, experiences, skills = _select_blocks(job, parsed, terms, feedback=feedback)
    doc = snippet_bank.reassemble(doc, r"PROJECTS[^}]*", projects)
    doc = snippet_bank.reassemble(doc, r"PROFESSIONAL EXPERIENCE", experiences)
    doc = snippet_bank.reassemble_skills(doc, skills)
    if parsed.heading_line:
        doc = doc.replace(parsed.heading_line, _tagline(), 1)
    return doc


# MacTeX's latexmk/pdflatex live here but aren't on PATH for non-interactive
# invocations (cron, this pipeline's own subprocess) -- see CLAUDE.md. Prepended,
# never replaces whatever PATH the process already has.
_EXTRA_TEX_PATHS = ["/Library/TeX/texbin", "/usr/local/texlive/2021/bin/universal-darwin"]


def _tex_env() -> dict:
    env = os.environ.copy()
    extra = [p for p in _EXTRA_TEX_PATHS if p not in env.get("PATH", "")]
    if extra:
        env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


_PAGE_COUNT_RE = re.compile(r"Output written on .*\((\d+) page")


def compile_tex(tex: str, out_dir: Path, name: str = "cv",
                 expected_pages: int | None = None) -> Path | None:
    """Compile tex to PDF via latexmk. Returns the PDF path, or None on failure
    (the .tex is still written for manual fixing).

    `expected_pages`, when given, treats a PDF that compiles but has the wrong
    page count as a failure too (cv_tailoring_workflow.md's "exactly 2 pages,
    not 2.1" rule) -- the PDF is still written to out_dir for manual review, it
    just isn't returned/marked ready. Only the unsupervised auto-tailor path
    passes this; the interactive CLI baseline is meant to be hand-edited
    afterward (see cv_tailoring_workflow.md Step 0), so it leaves this off."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{name}.tex"
    tex_path.write_text(tex, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_tex = Path(tmp) / f"{name}.tex"
        tmp_tex.write_text(tex, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["latexmk", "-pdf", "-interaction=nonstopmode", tmp_tex.name],
                cwd=tmp, capture_output=True, text=True, env=_tex_env(),
            )
        except OSError as exc:  # latexmk missing / not runnable
            (out_dir / f"{name}.compile.log").write_text(
                f"latexmk failed to run: {exc}\n"
                f"Install it, e.g. `brew install --cask basictex`.\n",
                encoding="utf-8",
            )
            return None
        tmp_pdf = Path(tmp) / f"{name}.pdf"
        if proc.returncode != 0 or not tmp_pdf.exists():
            (out_dir / f"{name}.compile.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            return None

        pdf_path = out_dir / f"{name}.pdf"
        shutil.copy(tmp_pdf, pdf_path)

        if expected_pages is not None:
            m = _PAGE_COUNT_RE.search(proc.stdout)
            pages = int(m.group(1)) if m else None
            if pages != expected_pages:
                (out_dir / f"{name}.compile.log").write_text(
                    f"Compiled to {pages if pages is not None else 'an unknown number of'} "
                    f"page(s), expected exactly {expected_pages}. PDF kept at {pdf_path} for "
                    f"review, but this needs a manual tailoring pass (see "
                    f"templates/cv_tailoring_workflow.md) before it's actually ready.\n",
                    encoding="utf-8",
                )
                return None

        return pdf_path


# Same environment quirk as latexmk (CLAUDE.md): poppler's CLI tools aren't
# reliably on PATH either, but a working `pdftotext` ships with the dalas conda
# env used for rendering. Best-effort only -- the fill-ratio check below just
# no-ops if it can't be found, it never blocks tailoring on its own.
_PDFTOTEXT_CANDIDATES = ["/Users/tuboshu/opt/anaconda3/envs/dalas/bin/pdftotext"]
_PDFTOTEXT_ENV_EXTRA = {"DYLD_LIBRARY_PATH": "/Users/tuboshu/opt/anaconda3/envs/dalas/lib"}


def _pdftotext_path() -> str | None:
    found = shutil.which("pdftotext")
    if found:
        return found
    for p in _PDFTOTEXT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _page_text(pdf_path: Path, page: int) -> str | None:
    exe = _pdftotext_path()
    if not exe:
        return None
    env = {**os.environ, **_PDFTOTEXT_ENV_EXTRA}
    try:
        proc = subprocess.run([exe, "-f", str(page), "-l", str(page), "-layout", str(pdf_path), "-"],
                               capture_output=True, text=True, env=env, timeout=15)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


_MIN_LAST_PAGE_FILL_RATIO = 0.4


def _last_page_fill_ratio(pdf_path: Path, n_pages: int) -> float | None:
    """Non-blank text lines on the last page vs the first, a cheap sparseness
    proxy that doesn't need vision -- page 1 of this template reliably packs
    edge-to-edge, so it's a reasonable self-calibrating baseline for "how full
    should a page look". None (no-op) if pdftotext isn't available or there's
    only 1 page to compare."""
    if n_pages < 2:
        return None
    first, last = _page_text(pdf_path, 1), _page_text(pdf_path, n_pages)
    if first is None or last is None:
        return None
    def _nonblank(t: str) -> int:
        return sum(1 for line in t.splitlines() if line.strip())
    base = _nonblank(first)
    return (_nonblank(last) / base) if base else None


def _retry_feedback(pdf: Path | None, out_dir: Path) -> str | None:
    """A hint for a second tailoring attempt, or None if the first attempt
    doesn't need one (it fit well) or can't be helped by retrying (a hard
    LaTeX error, not a length issue)."""
    if pdf is None:
        log = out_dir / "cv.compile.log"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        m = re.search(r"Compiled to (\d+) page", text)
        if not m:
            return None  # not a page-count rejection -- a real compile error, retrying won't help
        pages = int(m.group(1))
        return (f"The previous attempt compiled to {pages} pages, it needs to be exactly 2. "
                + ("Trim more bullets, or drop an entry." if pages > 2 else
                   "You have room to keep more bullets, or add a project back."))
    ratio = _last_page_fill_ratio(pdf, 2)
    if ratio is not None and ratio < _MIN_LAST_PAGE_FILL_RATIO:
        return (f"The previous attempt left the second page visibly sparse (about "
                 f"{ratio:.0%} as full as the first page). Keep more bullets, or add a "
                 f"project back, to fill it better -- but don't re-add anything you'd "
                 f"otherwise cut just to take up space.")
    return None


def tailor_job(job: Job, job_id: int, auto: bool = False) -> tuple[Path, Path | None]:
    """Generate + compile a tailored CV for a job. Returns (tex_path, pdf_path).

    `auto=True` is the unsupervised daily_run path: it also enforces the exact
    2-page rule via compile_tex's expected_pages, since nothing else reviews the
    output before it could be marked cv_ready, and retries once (with feedback
    from the first attempt, see _retry_feedback) if the page count is wrong or
    the second page looks sparse -- mirroring the look-then-adjust pass done
    when tailoring by hand. `auto=False` (the interactive CLI `tailor <job_id>`
    command) skips both -- its output is a starting point for a hand-editing
    pass, not a finished CV (cv_tailoring_workflow.md Step 0)."""
    out_dir = CV_OUT_DIR / f"{job_id}-{_slug(job.company)}"

    tex = tailor_tex(job)
    pdf = compile_tex(tex, out_dir, name="cv", expected_pages=2 if auto else None)

    if auto:
        feedback = _retry_feedback(pdf, out_dir)
        if feedback:
            tex = tailor_tex(job, feedback=feedback)
            pdf = compile_tex(tex, out_dir, name="cv", expected_pages=2)

    return out_dir / "cv.tex", pdf
