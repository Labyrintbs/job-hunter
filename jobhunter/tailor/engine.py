"""Tailor the base CV to a job and compile it to PDF."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .. import fetch_diag
from ..config import DATA_DIR, REPO_ROOT
from ..db import CV_FALLBACK_NOTE
from ..llm import provider
from ..models import Job
from . import select as llm_select
from . import snippet_bank
from .snippet_bank import Block, ParsedCV, SkillCategory

BASE_CV = REPO_ROOT / "templates" / "cv_base.tex"
CV_OUT_DIR = DATA_DIR / "cv"


def _job_terms(job: Job) -> set[str]:
    return snippet_bank.terms_in(f"{job.title} {job.description}")


# This automated path's own default caps -- the workflow doc's Step 2 ("what to
# cut") leaves the actual count to per-JD judgment, it doesn't fix a number.
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


def _select_blocks(job: Job, parsed: ParsedCV, terms: set[str], feedback: str | None = None,
                    judge_context: str | None = None
                    ) -> tuple[list[Block], list[Block], list[SkillCategory], bool]:
    """Decide which experiences/projects/skill categories (and which bullets
    within them) to keep, mirroring templates/cv_tailoring_workflow.md (same
    rules used tailoring by hand): an LLM call chooses from the real, existing
    content (see tailor/select.py), falling back to a deterministic
    keyword-overlap heuristic (whole blocks, no bullet trimming) if the LLM
    backend is unavailable or its response is unusable, so tailoring never
    hard-fails just because that call did. `feedback` (optional) is a hint from
    a previous compile attempt that didn't fit the page -- see tailor_job's
    retry. `judge_context` (optional) is the fit-judge's own verdict/reasons
    for this posting, already computed and stored -- passed through as extra
    background, not re-derived. Returns (projects, experiences, skills, used_fallback);
    each fallback is recorded via fetch_diag under a `tailor_llm_*` reason."""
    reason, detail = "tailor_llm_unavailable", "no LLM backend"
    if provider.available():
        try:
            result = llm_select.select(
                job,
                _menu_pairs(parsed.experiences),
                _menu_pairs(parsed.projects),
                [c.name for c in parsed.skills],
                feedback=feedback,
                judge_context=judge_context,
            )
            experiences = _apply_selection(parsed.experiences, result.get("experience_ids"),
                                            result.get("experience_bullets"), MAX_EXPERIENCES)
            projects = _apply_selection(parsed.projects, result.get("project_ids"),
                                         result.get("project_bullets"), MAX_PROJECTS)
            skills = _apply_names(parsed.skills, result.get("skill_categories"))
            if experiences and projects and skills:
                return projects, experiences, skills, False
            reason, detail = "tailor_llm_unusable", "LLM selection was empty or incomplete"
        except Exception as exc:
            reason, detail = "tailor_llm_error", f"{type(exc).__name__}: {exc}"[:200]

    fetch_diag.track("tailor", reason, detail=detail, company=job.company)
    return (_fallback_select(parsed.projects, terms, MAX_PROJECTS),
            _fallback_select(parsed.experiences, terms, MAX_EXPERIENCES),
            _fallback_select_skills(parsed.skills, terms), True)


# Update when the target start date changes (e.g. back to "from <Month Year>")
# -- kept as one constant so it's never silently dropped by a re-tailor (see
# templates/cv_tailoring_workflow.md).
AVAILABILITY = "available immediately"


def _tagline(role_category: str = "") -> str:
    # Deliberately generic (no per-job targeting clause), two fixed variants
    # only by role_category -- see templates/cv_tailoring_workflow.md. Default
    # variant matches templates/cv_base.tex's own heading line.
    if role_category == "PM":
        return (
            f"{{Seeking an AI Product Manager role (CDI/CDD), "
            f"{AVAILABILITY} — Île-de-France, open to mobility}}"
        )
    return (
        f"{{Seeking a Machine Learning role (CDI/CDD), {AVAILABILITY} — "
        f"Île-de-France, open to mobility}}"
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def _tailor(job: Job, parsed: ParsedCV | None = None, feedback: str | None = None,
            judge_context: str | None = None, role_category: str = "") -> tuple[str, bool]:
    """(tex, used_keyword_fallback)."""
    parsed = parsed or snippet_bank.parse(BASE_CV)
    terms = _job_terms(job)
    doc = parsed.document

    projects, experiences, skills, used_fallback = _select_blocks(
        job, parsed, terms, feedback=feedback, judge_context=judge_context)
    doc = snippet_bank.reassemble(doc, r"PROJECTS[^}]*", projects)
    doc = snippet_bank.reassemble(doc, r"PROFESSIONAL EXPERIENCE", experiences)
    doc = snippet_bank.reassemble_skills(doc, skills)
    if parsed.heading_line:
        doc = doc.replace(parsed.heading_line, _tagline(role_category), 1)
    return doc, used_fallback


def tailor_tex(job: Job, parsed: ParsedCV | None = None, feedback: str | None = None,
               judge_context: str | None = None, role_category: str = "") -> str:
    return _tailor(job, parsed, feedback, judge_context, role_category)[0]


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

        # A prior attempt (e.g. before a retry) may have left a failure log behind --
        # clear it so success never leaves stale "needs a manual pass" evidence sitting
        # next to a PDF that's actually fine.
        (out_dir / f"{name}.compile.log").unlink(missing_ok=True)
        return pdf_path


# Same environment quirk as latexmk (CLAUDE.md) -- poppler's CLI tools aren't
# reliably on PATH either, but pdftotext ships with the dalas conda env used
# for rendering. Best-effort: the fill-ratio check below just no-ops if not found.
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
    only 1 page to compare; the pdftotext case is recorded via fetch_diag so
    sparse CVs don't pass silently."""
    if n_pages < 2:
        return None
    first, last = _page_text(pdf_path, 1), _page_text(pdf_path, n_pages)
    if first is None or last is None:
        fetch_diag.track("tailor", "tailor_fill_check_unavailable", detail="pdftotext missing or failed")
        return None
    def _nonblank(t: str) -> int:
        return sum(1 for line in t.splitlines() if line.strip())
    base = _nonblank(first)
    return (_nonblank(last) / base) if base else None


def _retry_feedback(pdf: Path | None, out_dir: Path, name: str = "cv") -> str | None:
    """A hint for a second tailoring attempt, or None if the first attempt
    doesn't need one (it fit well) or can't be helped by retrying (a hard
    LaTeX error, not a length issue)."""
    if pdf is None:
        log = out_dir / f"{name}.compile.log"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        m = re.search(r"Compiled to (\d+) page", text)
        if not m:
            return None  # not a page-count rejection -- a real compile error, retrying won't help
        pages = int(m.group(1))
        return (f"The previous attempt compiled to {pages} pages, it needs to be exactly 2. "
                + ("Trim bullets roughly evenly across the kept experience entries, or drop "
                   "a whole entry/project, rather than cutting one entry's bullets down far "
                   "more than the others -- an entry left with noticeably fewer bullets than "
                   "its neighbors reads as sparse even when the total page count is right."
                   if pages > 2 else
                   "You have room to keep more bullets, or add a project back."))
    ratio = _last_page_fill_ratio(pdf, 2)
    if ratio is not None and ratio < _MIN_LAST_PAGE_FILL_RATIO:
        return (f"The previous attempt left the second page visibly sparse (about "
                 f"{ratio:.0%} as full as the first page). Keep more bullets, or add a "
                 f"project back, to fill it better -- but don't re-add anything you'd "
                 f"otherwise cut just to take up space.")
    return None


class TailorResult(NamedTuple):
    tex_path: Path
    pdf_path: Path | None
    note: str = ""   # why the CV failed or needs review; "" when fine

    @property
    def fallback(self) -> bool:
        return self.note.startswith(CV_FALLBACK_NOTE)


def _compile_failure(out_dir: Path, name: str = "cv") -> tuple[str, str]:
    """(fetch_diag reason, note) for a compile that returned no PDF, read off the
    log compile_tex wrote."""
    log = out_dir / f"{name}.compile.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    m = re.search(r"Compiled to (\d+|an unknown number of) page", text)
    if m:
        return "tailor_page_count", f"compiled to {m.group(1)} page(s), expected exactly 2 -- see {name}.compile.log"
    return "tailor_latex_error", f"LaTeX compile error -- see {name}.compile.log"


def _version_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _publish_latest(out_dir: Path, name: str) -> None:
    """Mirror a versioned tailoring to the fixed cv.tex / cv.pdf / cv.compile.log
    names, which the hand-editing workflow (cv_tailoring_workflow.md) opens."""
    for ext in ("tex", "pdf"):
        src = out_dir / f"{name}.{ext}"
        if src.exists():
            shutil.copy(src, out_dir / f"cv.{ext}")
    log, latest_log = out_dir / f"{name}.compile.log", out_dir / "cv.compile.log"
    if log.exists():
        shutil.copy(log, latest_log)
    else:
        latest_log.unlink(missing_ok=True)


def tailor_job(job: Job, job_id: int, auto: bool = False,
              judge_context: str | None = None, role_category: str = "") -> TailorResult:
    """Generate + compile a tailored CV for a job. Returns a TailorResult; a failed
    compile, or a still-sparse second page after the retry, sets `note` and is
    recorded via fetch_diag under a `tailor_*` reason.

    `auto=True` is the unsupervised daily_run path: it also enforces the exact
    2-page rule via compile_tex's expected_pages, since nothing else reviews the
    output before it could be marked cv_ready, and retries once (with feedback
    from the first attempt, see _retry_feedback) if the page count is wrong or
    the second page looks sparse -- mirroring the look-then-adjust pass done
    when tailoring by hand. `auto=False` (the interactive CLI `tailor <job_id>`
    command) skips both -- its output is a starting point for a hand-editing
    pass, not a finished CV (cv_tailoring_workflow.md Step 0). `judge_context`
    (optional) is the fit-judge's own verdict/reasons for this posting, passed
    through to the block-selection call as background.

    Every tailoring is written under its own timestamped name (cv-<stamp>.tex/.pdf),
    which is what the returned paths point at, so a re-tailor never overwrites an
    earlier version; cv.tex/cv.pdf are then refreshed as the latest working copy."""
    out_dir = CV_OUT_DIR / f"{job_id}-{_slug(job.company)}"
    name = f"cv-{_version_stamp()}"

    tex, used_fallback = _tailor(job, judge_context=judge_context, role_category=role_category)
    pdf = compile_tex(tex, out_dir, name=name, expected_pages=2 if auto else None)

    retried = False
    if auto:
        feedback = _retry_feedback(pdf, out_dir, name)
        if feedback:
            tex, used_fallback = _tailor(job, feedback=feedback, judge_context=judge_context,
                                         role_category=role_category)
            pdf = compile_tex(tex, out_dir, name=name, expected_pages=2)
            retried = True

    reason = note = ""
    if pdf is None:
        reason, note = _compile_failure(out_dir, name)
    elif retried:
        # The retry's own output is never re-tried, so flag a still-sparse page 2
        # instead of letting it pass as fine.
        ratio = _last_page_fill_ratio(pdf, 2)
        if ratio is not None and ratio < _MIN_LAST_PAGE_FILL_RATIO:
            reason, note = "tailor_sparse_after_retry", f"page 2 sparse ({ratio:.0%} of page 1) -- review before use"
    if reason:
        fetch_diag.track("tailor", reason, detail=note, company=job.company)
    if used_fallback:
        note = f"{CV_FALLBACK_NOTE}; {note}" if note else CV_FALLBACK_NOTE
    _publish_latest(out_dir, name)
    return TailorResult(out_dir / f"{name}.tex", pdf, note)
