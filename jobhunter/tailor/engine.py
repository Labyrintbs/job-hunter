"""Tailor the base CV to a job and compile it to PDF."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .. import fetch_diag
from ..config import DATA_DIR, REPO_ROOT
from ..db import CV_FALLBACK_NOTE
from ..lang import counts, job_language
from ..llm import provider
from ..models import Job
from . import select as llm_select
from . import courses, fit, snippet_bank, summary
from .snippet_bank import Block, ParsedCV, SkillCategory

BASE_CV = REPO_ROOT / "templates" / "cv_base.tex"
BASE_CV_FR = REPO_ROOT / "templates" / "cv_base_fr.tex"
CV_OUT_DIR = DATA_DIR / "cv"


def base_cv_path(language: str = "en"):
    """The master CV for a language; English unless the job's text is French."""
    return BASE_CV_FR if language == "fr" else BASE_CV


def master_edited_at(language: str = "en") -> str:
    """When the master CV was last edited, as a UTC 'YYYY-MM-DD HH:MM:SS' string
    comparable with cv_artifacts.generated_at. A CV older than this predates the master."""
    mtime = base_cv_path(language).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _job_terms(job: Job) -> set[str]:
    return snippet_bank.terms_in(f"{job.title} {job.description}")


# This automated path's own default caps -- the workflow doc's Step 2 ("what to
# cut") leaves the actual count to per-JD judgment, it doesn't fix a number.
MAX_EXPERIENCES = 2
MAX_PROJECTS = 3

# Only ever dropped for a job with no vision/medical signal -- the one category
# every manually-tailored CV this session has actually dropped. See
# cv_tailoring_workflow.md's "Emphasis" step.
_CONDITIONAL_SKILL_CATEGORY = {"en": r"Computer Vision \& Medical Imaging",
                               "fr": "Vision par ordinateur et imagerie médicale"}


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


def _pick(blocks: list[Block], ids: object, scores: object, cap: int) -> list[tuple[Block, list]]:
    """The chosen blocks with their bullet scores, in the order the LLM gave (it was told
    to default to reverse-chronological and only deviate with a stated reason -- trust
    that judgment rather than re-sorting). Ignores out-of-range/duplicate/malformed ids
    rather than raising, since a slightly messy response shouldn't crash tailoring."""
    if not isinstance(ids, list):
        return []
    scores = scores if isinstance(scores, list) else []
    seen: set[int] = set()
    chosen = []
    for pos, i in enumerate(ids):
        if isinstance(i, int) and 0 <= i < len(blocks) and i not in seen:
            seen.add(i)
            mine = scores[pos] if pos < len(scores) and isinstance(scores[pos], list) else []
            chosen.append((blocks[i], [s for s in mine if isinstance(s, (int, float))]))
    return chosen[:cap]


def _menu_pairs(blocks: list[Block]) -> list[tuple[str, list[str]]]:
    return [(b.text, b.bullets()) for b in blocks]


# Skills lines that are never trimmed or scored.
_FIXED_SKILL_CATEGORY = {"en": "Languages", "fr": "Langues"}
_CONDITIONAL_MAX_SCORE = 25     # the conditional category is dropped when no item scores above this


class Selection(NamedTuple):
    experiences: list[Block]
    projects: list[Block]
    skills: list[SkillCategory]
    plan: fit.Plan
    used_fallback: bool


def _keyword_bullet_scores(text: str, terms: set[str]) -> int:
    return min(100, 30 + 15 * len(snippet_bank.terms_in(text) & terms))


MAX_EXTRA_PROJECTS = 2     # spare projects, shown only when the page turns out to have room


def _with_spares(primary: list, extras: list) -> list[tuple[Block, list, bool]]:
    """[(Block, scores, shown)]: the chosen projects in their order, with each spare slotted in
    by date so it reads in place if the fit step adds it."""
    out = [(b, s, True) for b, s in primary]
    taken = {id(b) for b, _, _ in out}
    for b, s in extras[:MAX_EXTRA_PROJECTS]:
        if id(b) in taken:
            continue
        pos = next((i for i, (o, _, _) in enumerate(out) if o.end_date() < b.end_date()), len(out))
        out.insert(pos, (b, s, False))
    return out


def _plan_for(experiences, projects, skills, lang: str) -> fit.Plan:
    """`experiences` is [(Block, bullet scores)], `projects` is [(Block, scores, shown)] and
    `skills` is [(SkillCategory, item scores or None)]."""
    def entry(block, scores, shown=True):
        e = fit.Entry.build(block.text, block.bullets(), scores)
        e.kept = shown
        return e
    lines = []
    for cat, scores in skills:
        parts = snippet_bank.split_skill_items(cat.line)
        fixed = cat.name == _FIXED_SKILL_CATEGORY[lang] or parts is None
        main = not lines and not fixed            # the first skills line (Technical Skills) matters most
        lines.append(fit.SkillLine.build(parts.items if parts else [], scores, trimmable=not fixed,
                                         seps=parts.seps if parts else None, main=main))
    return fit.Plan([entry(b, s) for b, s in experiences], [entry(b, s, k) for b, s, k in projects], lines)


def _select_blocks(job: Job, parsed: ParsedCV, terms: set[str],
                   judge_context: str | None = None) -> Selection:
    """Decide which experiences/projects to keep and score every bullet and skills item,
    mirroring templates/cv_tailoring_workflow.md: an LLM call chooses from the real,
    existing content (see tailor/select.py), falling back to deterministic keyword
    scores if the LLM backend is unavailable or its response is unusable, so tailoring
    never hard-fails just because that call did. How much of it fits is decided later,
    by measuring (tailor/fit.py). `judge_context` (optional) is the fit-judge's own
    verdict/reasons for this posting, already computed and stored. Each fallback is
    recorded via fetch_diag under a `tailor_llm_*` reason."""
    lang = parsed.lang
    conditional = _CONDITIONAL_SKILL_CATEGORY[lang]
    menu_cats = [c for c in parsed.skills if c.name != _FIXED_SKILL_CATEGORY[lang]]
    reason, detail = "tailor_llm_unavailable", "no LLM backend"
    if provider.available():
        try:
            result = llm_select.select(
                job,
                _menu_pairs(parsed.experiences),
                _menu_pairs(parsed.projects),
                [(c.name, split.items) for c in menu_cats if (split := snippet_bank.split_skill_items(c.line))],
                judge_context=judge_context,
            )
            exps = _pick(parsed.experiences, result.get("experience_ids"), result.get("experience_scores"),
                         MAX_EXPERIENCES)
            projs = _pick(parsed.projects, result.get("project_ids"), result.get("project_scores"),
                          MAX_PROJECTS)
            extras = _pick(parsed.projects, result.get("extra_project_ids"),
                           result.get("extra_project_scores"), MAX_EXTRA_PROJECTS)
            if exps and projs:
                projs = _with_spares(projs, extras)
                raw = result.get("skill_scores")
                raw = raw if isinstance(raw, list) else []
                by_cat = {c.name: (raw[i] if i < len(raw) and isinstance(raw[i], list) else None)
                          for i, c in enumerate(menu_cats)}
                skills = []
                for c in parsed.skills:
                    scores = by_cat.get(c.name)
                    if c.name == conditional and scores and max(scores) < _CONDITIONAL_MAX_SCORE:
                        continue
                    skills.append((c, scores))
                return Selection([b for b, _ in exps], [b for b, _, _ in projs], [c for c, _ in skills],
                                 _plan_for(exps, projs, skills, lang), False)
            reason, detail = "tailor_llm_unusable", "LLM selection was empty or incomplete"
        except Exception as exc:
            reason, detail = "tailor_llm_error", f"{type(exc).__name__}: {exc}"[:200]

    fetch_diag.track("tailor", reason, detail=detail, company=job.company)
    exps = [(b, [_keyword_bullet_scores(x, terms) for x in b.bullets()])
            for b in _fallback_select(parsed.experiences, terms, MAX_EXPERIENCES)]
    def keyword_scored(blocks):
        return [(b, [_keyword_bullet_scores(x, terms) for x in b.bullets()]) for b in blocks]
    chosen = _fallback_select(parsed.projects, terms, MAX_PROJECTS)
    spare = [b for b in _fallback_select(parsed.projects, terms, MAX_PROJECTS + MAX_EXTRA_PROJECTS)
             if all(b is not c for c in chosen)]
    projs = _with_spares(keyword_scored(chosen), keyword_scored(spare))
    skills = []
    for c in parsed.skills:
        if c.name == conditional and not (c.tags & terms):
            continue
        split = snippet_bank.split_skill_items(c.line)
        scores = None if c.name == _FIXED_SKILL_CATEGORY[lang] or not split else [
            _keyword_bullet_scores(i, terms) for i in split.items]
        skills.append((c, scores))
    return Selection([b for b, _ in exps], [b for b, _, _ in projs], [c for c, _ in skills],
                     _plan_for(exps, projs, skills, lang), True)


# Update when the target start date changes (e.g. back to "from <Month Year>")
# -- kept as one constant per language so it's never silently dropped by a
# re-tailor (see templates/cv_tailoring_workflow.md).
AVAILABILITY = {"en": "available immediately", "fr": "disponible immédiatement"}


def _tagline(role_category: str = "", lang: str = "en") -> str:
    # Deliberately generic (no per-job targeting clause), two fixed variants
    # only by role_category -- see templates/cv_tailoring_workflow.md. Default
    # variant matches the base CV's own heading line (templates/cv_base*.tex).
    pm = role_category == "PM"
    if lang == "fr":
        role = "Product Manager IA" if pm else "Ingénieur Machine Learning"
        return f"{{{role} (CDI/CDD), {AVAILABILITY['fr']} — Île-de-France, ouvert à la mobilité}}"
    role = "an AI Product Manager" if pm else "a Machine Learning"
    return f"{{Seeking {role} role (CDI/CDD), {AVAILABILITY['en']} — Île-de-France, open to mobility}}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def _write_summary(job: Job, role_category: str, parsed: ParsedCV) -> summary.Summary:
    summ = summary.build(job, role_category, parsed.document, parsed.lang)
    if summ.reason:
        fetch_diag.track("tailor", summ.reason, detail=summ.detail, company=job.company)
    return summ


class Draft(NamedTuple):
    """Everything chosen for one tailoring; `render` turns a (possibly trimmed) plan into LaTeX."""
    job: Job
    parsed: ParsedCV
    selection: Selection
    summ: summary.Summary
    role_category: str

    @property
    def plan(self) -> fit.Plan:
        return self.selection.plan

    @property
    def notes(self) -> list[str]:
        return [f"summary fell back to the standard text ({self.summ.detail})"] if self.summ.reason else []

    def render(self, plan: fit.Plan | None = None) -> str:
        plan = plan or self.plan
        lang, sel, parsed = self.parsed.lang, self.selection, self.parsed
        names = snippet_bank.SECTIONS[lang]

        def trimmed(blocks, entries):
            out = []
            for block, e in zip(blocks, entries):
                if not e.kept:
                    continue
                keep = [j for j, k in enumerate(e.keep) if k]
                out.append(Block(snippet_bank.filter_bullets(block.text, keep) if keep else block.text, block.tags))
            return out

        skills = []
        for cat, line in zip(sel.skills, plan.skills):
            parts = snippet_bank.split_skill_items(cat.line)
            text = snippet_bank.join_skill_items(parts, line.keep) if parts and line.trimmable else cat.line
            skills.append(SkillCategory(cat.name, text, cat.tags))
        doc = snippet_bank.reassemble(parsed.document, names["projects"], trimmed(sel.projects, plan.projects))
        doc = snippet_bank.reassemble(doc, names["experience"], trimmed(sel.experiences, plan.experiences))
        doc = snippet_bank.reassemble_skills(doc, skills, lang)
        if parsed.heading_line:
            doc = doc.replace(parsed.heading_line, _tagline(self.role_category, lang), 1)
        doc = snippet_bank.set_summary(doc, summary.latex_escape(self.summ.text))
        return courses.apply(doc, f"{self.job.title} {self.job.description}", keep=plan.modules)


def _draft(job: Job, parsed: ParsedCV | None = None, judge_context: str | None = None,
           role_category: str = "", language: str = "en",
           summ: summary.Summary | None = None) -> Draft:
    """`language` picks the master CV ("en" or "fr"); an already parsed CV carries its
    own. `summ` reuses an already written summary (it doesn't depend on the selection)."""
    parsed = parsed or snippet_bank.parse(base_cv_path(language), language)
    selection = _select_blocks(job, parsed, _job_terms(job), judge_context=judge_context)
    return Draft(job, parsed, selection, summ or _write_summary(job, role_category, parsed), role_category)


_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+\*?|[{}\\$]|%[^\n]*")


def cv_language_problem(tex: str, expected: str) -> str:
    """"" when the assembled CV reads in `expected` ("en" or "fr"), else a note.
    Counts function words over the whole body, so technical terms in the other
    language don't matter; the expected language must clearly dominate."""
    fr_hits, en_hits = counts(_LATEX_COMMAND.sub(" ", tex.split(r"\begin{document}")[-1]))
    mine, other = (fr_hits, en_hits) if expected == "fr" else (en_hits, fr_hits)
    if mine >= 2 * other:
        return ""
    return (f"language check: the CV text reads as {'EN' if expected == 'fr' else 'FR'} "
            f"({other} vs {mine} function words), expected {expected.upper()}")


def tailor_tex(job: Job, parsed: ParsedCV | None = None, judge_context: str | None = None,
               role_category: str = "", language: str = "en") -> str:
    """The tailored CV before it is fitted to two pages (every chosen bullet kept)."""
    return _draft(job, parsed, judge_context, role_category, language).render()


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


def _page_layout(pdf_path: Path) -> fit.Layout | None:
    """Where every line of text sits on every page, or None when it can't be read
    (no pdftotext, or no PDF); the fit step then can't measure and says so."""
    exe = _pdftotext_path()
    if not exe:
        return None
    env = {**os.environ, **_PDFTOTEXT_ENV_EXTRA}
    try:
        proc = subprocess.run([exe, "-bbox", str(pdf_path), "-"], capture_output=True, text=True,
                              env=env, timeout=30)
    except Exception:
        return None
    return fit.parse_bbox(proc.stdout) if proc.returncode == 0 else None


class TailorResult(NamedTuple):
    tex_path: Path
    pdf_path: Path | None
    note: str = ""   # why the CV failed or needs review; "" when fine
    lang: str = "en"

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


def _fit_cv(draft: Draft, out_dir: Path, name: str):
    """Fit the draft to two pages (see tailor/fit.py) and keep a log of what it did next
    to the CV. Trial compiles go to a scratch directory; the caller compiles the result."""
    with tempfile.TemporaryDirectory() as tmp:
        result = fit.fit(draft.plan, draft.render,
                         lambda tex: compile_tex(tex, Path(tmp), name="fit"), _page_layout)
    lay = result.layout
    lines = [f"status: {result.status} after {result.compiles} compile(s)"]
    if lay:
        lines.append(f"pages: {lay.pages}; free lines per page: "
                     + ", ".join(f"{lay.free_lines(p):.1f}" for p in range(lay.pages)))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.fit.txt").write_text("\n".join(lines + result.log) + "\n", encoding="utf-8")
    return result


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
              judge_context: str | None = None, role_category: str = "",
              language: str | None = None) -> TailorResult:
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

    language = language or job_language(job.title, job.description, job.language)
    draft = _draft(job, judge_context=judge_context, role_category=role_category, language=language)
    used_fallback, notes = draft.selection.used_fallback, draft.notes
    fitted = _fit_cv(draft, out_dir, name)
    if fitted.status == "unmeasured":
        fetch_diag.track("tailor", "tailor_fit_unmeasured", detail="could not read the page layout", company=job.company)
    if fitted.note:
        notes.append(fitted.note)
    tex = fitted.tex
    pdf = compile_tex(tex, out_dir, name=name, expected_pages=2 if auto else None)

    reason = note = ""
    if pdf is None:
        reason, note = _compile_failure(out_dir, name)
    wrong_language = cv_language_problem(tex, language)
    if wrong_language and not reason:
        reason, note = summary.LANGUAGE_REASON, wrong_language
    if reason:
        fetch_diag.track("tailor", reason, detail=note, company=job.company)
    if notes:   # summary fallbacks: already tracked by the summary itself, shown as a review note
        note = "; ".join(x for x in (note, *notes) if x)
    if used_fallback:
        note = f"{CV_FALLBACK_NOTE}; {note}" if note else CV_FALLBACK_NOTE
    _publish_latest(out_dir, name)
    return TailorResult(out_dir / f"{name}.tex", pdf, note, language)
