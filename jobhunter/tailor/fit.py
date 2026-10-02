"""Fit a tailored CV to exactly two pages by measuring it, not by guessing.

The selection call (tailor/select.py) only scores content. This module starts from the
full CV, compiles it, reads where every line landed (pdftotext -bbox, parsed here) and
removes the lowest-scored items until it fits; if room is left it adds the best removed
items back, verifying each addition by compiling. Content is protected in tiers: Major
Modules go first, then skills items, then project bullets, then a whole project, and
work-experience bullets last. Everything is injected (render / compile / measure), so the
loop is testable without LaTeX.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHARS_PER_LINE = 105          # rough width of a bullet line, used only to estimate lines freed
SKILL_CHARS_PER_LINE = 115
BOTTOM_MARGIN_PT = 36         # 0.5in bottom margin in both masters

# Added to a score: work-experience bullets are removed only after everything else is gone
# (100 outweighs any score), projects outlast skills items. Major Modules always go first.
PROTECT = {"skill": 0, "project_bullet": 20, "project": 30, "exp_bullet": 100}
MODULE_EFF = -20
MAX_MODULES, MIN_MODULES = 4, 2
MIN_PROJECTS = 2
MIN_PROJECT_BULLETS = 1
MIN_EXP_BULLETS = 2
MIN_SKILL_ITEMS = 3          # a skills line never drops below this many items...
SKILL_KEEP_SHARE = 0.4       # ...or below this share of its items, whichever is more
MODULE_LINES = 0.4

MAX_COMPILES = 10
ADD_BACK_MIN_FREE_LINES = 2.0     # add back only when at least this much room is left


def plain_len(text: str) -> int:
    return len(re.sub(r"\\[A-Za-z]+\*?|[{}$\\]", "", text))


# ---------------------------------------------------------------------------- layout

@dataclass
class Layout:
    pages: int
    height: float
    pitch: float                # distance between two lines of body text, in points
    bottoms: list[float]        # bottom of the last text line of each page
    line_counts: list[int]
    first_words: list[str]

    @property
    def limit(self) -> float:
        return self.height - BOTTOM_MARGIN_PT

    def overflow_lines(self, pages: int = 2) -> int:
        return sum(self.line_counts[pages:])

    def free_lines(self, page: int) -> float:
        """Room left at the bottom of a page (0-based), in lines of body text."""
        if page >= self.pages:
            return 0.0
        return max(0.0, (self.limit - self.bottoms[page]) / self.pitch)


_PAGE_RE = re.compile(r'<page width="[\d.]+" height="([\d.]+)">(.*?)</page>', re.S)
_WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="[\d.]+" yMax="([\d.]+)">(.*?)</word>')


def parse_bbox(text: str | None) -> Layout | None:
    """A Layout from `pdftotext -bbox` output; None if it has no pages or words."""
    if not text:
        return None
    pages = _PAGE_RE.findall(text)
    if not pages:
        return None
    height = float(pages[0][0])
    bottoms, counts, firsts, gaps = [], [], [], []
    for _, body in pages:
        words = [(float(y0), float(x0), float(y1), w) for x0, y0, y1, w in _WORD_RE.findall(body)]
        if not words:
            bottoms.append(0.0), counts.append(0), firsts.append("")
            continue
        words.sort()
        lines = [words[0][0]]
        for y0, *_ in words[1:]:
            if y0 - lines[-1] > 3:
                lines.append(y0)
        counts.append(len(lines))
        bottoms.append(max(w[2] for w in words))
        firsts.append(words[0][3])
        gaps += [b - a for a, b in zip(lines, lines[1:]) if b - a <= 20]
    pitch = statistics.median(gaps) if gaps else 12.0
    return Layout(len(pages), height, pitch, bottoms, counts, firsts)


# ------------------------------------------------------------------------------ plan

@dataclass
class Entry:
    """An experience or a project: scored bullets, plus (for projects) whole-entry removal."""
    lines: list[float]               # estimated lines per bullet
    scores: list[int]
    keep: list[bool]
    base_lines: float = 3.0          # heading and description
    kept: bool = True

    @classmethod
    def build(cls, block_text: str, bullets: list[str], scores: list[int] | None) -> "Entry":
        scores = list(scores or [])[:len(bullets)]
        scores += [50] * (len(bullets) - len(scores))
        head = max(0, plain_len(block_text) - sum(plain_len(b) for b in bullets))
        return cls([max(1, math.ceil(plain_len(b) / CHARS_PER_LINE)) for b in bullets], scores,
                   [True] * len(bullets), 2.0 + head / CHARS_PER_LINE)

    def kept_count(self) -> int:
        return sum(self.keep)


@dataclass
class SkillLine:
    chars: list[int]
    scores: list[int]
    keep: list[bool]
    trimmable: bool = True
    min_keep: int = MIN_SKILL_ITEMS

    @classmethod
    def build(cls, items: list[str], scores: list[int] | None, trimmable: bool = True) -> "SkillLine":
        scores = list(scores or [])[:len(items)]
        scores += [50] * (len(items) - len(scores))
        return cls([plain_len(i) + 2 for i in items], scores, [True] * len(items), trimmable,
                   max(MIN_SKILL_ITEMS, math.ceil(SKILL_KEEP_SHARE * len(items))))


@dataclass
class Plan:
    experiences: list[Entry]
    projects: list[Entry]
    skills: list[SkillLine]
    modules: int = MAX_MODULES

    def copy(self) -> "Plan":
        def entry(e):
            return Entry(e.lines, e.scores, list(e.keep), e.base_lines, e.kept)

        def skill(s):
            return SkillLine(s.chars, s.scores, list(s.keep), s.trimmable, s.min_keep)
        return Plan([entry(e) for e in self.experiences], [entry(e) for e in self.projects],
                    [skill(s) for s in self.skills], self.modules)


@dataclass(frozen=True)
class Cut:
    kind: str          # module | skill | project_bullet | project | exp_bullet
    a: int = 0         # entry / skill line index
    b: int = 0         # bullet / item index
    eff: float = 0.0
    lines: float = 0.0
    keep: tuple | None = None    # a project added back with only some of its bullets

    @property
    def key(self) -> tuple:
        return (self.kind, self.a, self.b)


def _project_eff(e: Entry) -> float:
    kept = [s for s, k in zip(e.scores, e.keep) if k]
    return (sum(kept) / len(kept) if kept else 50) + PROTECT["project"]


def _project_lines(e: Entry) -> float:
    return e.base_lines + sum(l for l, k in zip(e.lines, e.keep) if k)


def removals(plan: Plan) -> list[Cut]:
    """Everything that may still be removed, least valuable first."""
    cuts = []
    if plan.modules > MIN_MODULES:
        cuts.append(Cut("module", eff=MODULE_EFF, lines=MODULE_LINES))
    for i, line in enumerate(plan.skills):
        if line.trimmable and sum(line.keep) > line.min_keep:
            cuts += [Cut("skill", i, j, line.scores[j] + PROTECT["skill"], line.chars[j] / SKILL_CHARS_PER_LINE)
                     for j, k in enumerate(line.keep) if k]
    for i, e in enumerate(plan.experiences):
        if e.kept and e.kept_count() > MIN_EXP_BULLETS:
            cuts += [Cut("exp_bullet", i, j, e.scores[j] + PROTECT["exp_bullet"], e.lines[j])
                     for j, k in enumerate(e.keep) if k]
    live = [i for i, e in enumerate(plan.projects) if e.kept]
    for i in live:
        e = plan.projects[i]
        if e.kept_count() > MIN_PROJECT_BULLETS:
            cuts += [Cut("project_bullet", i, j, e.scores[j] + PROTECT["project_bullet"], e.lines[j])
                     for j, k in enumerate(e.keep) if k]
        if len(live) > MIN_PROJECTS:
            cuts.append(Cut("project", i, 0, _project_eff(e), _project_lines(e)))
    return sorted(cuts, key=lambda c: (c.eff, -c.a, -c.b))


def additions(plan: Plan) -> list[Cut]:
    """Everything removed so far that could be put back, most valuable first."""
    cuts = []
    if plan.modules < MAX_MODULES:
        cuts.append(Cut("module", eff=MODULE_EFF, lines=MODULE_LINES))
    for i, line in enumerate(plan.skills):
        cuts += [Cut("skill", i, j, line.scores[j] + PROTECT["skill"], line.chars[j] / SKILL_CHARS_PER_LINE)
                 for j, k in enumerate(line.keep) if not k]
    for kind, entries in (("exp_bullet", plan.experiences), ("project_bullet", plan.projects)):
        for i, e in enumerate(entries):
            if e.kept:
                cuts += [Cut(kind, i, j, e.scores[j] + PROTECT[kind], e.lines[j])
                         for j, k in enumerate(e.keep) if not k]
    cuts += [Cut("project", i, 0, _project_eff(e), _project_lines(e))
             for i, e in enumerate(plan.projects) if not e.kept]
    return sorted(cuts, key=lambda c: (-c.eff, c.a, c.b))


def apply_cut(plan: Plan, cut: Cut, add: bool = False) -> None:
    if cut.kind == "module":
        plan.modules += 1 if add else -1
    elif cut.kind == "skill":
        plan.skills[cut.a].keep[cut.b] = add
    elif cut.kind == "project":
        plan.projects[cut.a].kept = add
        if add and cut.keep is not None:
            plan.projects[cut.a].keep = list(cut.keep)
    else:
        entries = plan.experiences if cut.kind == "exp_bullet" else plan.projects
        entries[cut.a].keep[cut.b] = add


def describe(cut: Cut) -> str:
    where = {"module": "a Major Module", "skill": f"skills item {cut.a}.{cut.b}",
             "project_bullet": f"project {cut.a} bullet {cut.b}", "project": f"project {cut.a}",
             "exp_bullet": f"experience {cut.a} bullet {cut.b}"}[cut.kind]
    return f"{where} (score {cut.eff:.0f})"


# ------------------------------------------------------------------------------- fit

@dataclass
class FitResult:
    status: str                    # fit | cannot_fit | unmeasured | compile_error
    plan: Plan
    tex: str
    layout: Layout | None
    compiles: int
    log: list[str] = field(default_factory=list)
    note: str = ""                 # worth a look on the board; "" for routine trimming


def _shrunk_project(plan: Plan, cut: Cut, limit: float) -> Cut | None:
    """A project that does not fit whole, added with only its best-scored bullets (at least
    one) that do; None when even that is too big."""
    e = plan.projects[cut.a]
    keep, used = [False] * len(e.keep), e.base_lines
    for j in sorted(range(len(e.keep)), key=lambda j: -e.scores[j]):
        if used + e.lines[j] <= limit or not any(keep):
            keep[j] = True
            used += e.lines[j]
    if used > limit + 0.25:
        return None
    return Cut("project", cut.a, 0, cut.eff, used, tuple(keep))


def _next_addition(plan: Plan, layout: Layout, failed: set) -> Cut | None:
    free = [layout.free_lines(p) for p in range(2)]
    biggest, total = max(free), sum(free)
    if total < ADD_BACK_MIN_FREE_LINES:
        return None
    options = [c for c in additions(plan) if c.key not in failed]
    for limit in (biggest, total):
        for c in options:
            if c.lines <= limit + 0.25:
                return c
            if c.kind == "project" and (shrunk := _shrunk_project(plan, c, limit)):
                return shrunk
    return None


def fit(plan: Plan, render: Callable[[Plan], str], compile_: Callable[[str], Path | None],
        measure: Callable[[Path], Layout | None], max_compiles: int = MAX_COMPILES) -> FitResult:
    """Trim `plan` until render(plan) compiles to exactly two pages, then fill leftover room."""
    cur = plan.copy()
    log: list[str] = []
    tex = render(cur)
    compiles = 1
    pdf = compile_(tex)
    if pdf is None:
        return FitResult("compile_error", cur, tex, None, compiles, log)
    layout = measure(pdf)
    if layout is None:
        return FitResult("unmeasured", cur, tex, None, compiles, log)

    failed: set = set()
    while True:
        if layout.pages > 2:
            need = layout.overflow_lines() + 0.5
            cuts, freed = [], 0.0
            while freed < need and compiles < max_compiles:
                options = removals(cur)       # recomputed after every cut, so no minimum is ever crossed
                if not options:
                    break
                cuts.append(options[0])
                freed += options[0].lines
                apply_cut(cur, options[0])
            if not cuts or compiles >= max_compiles:
                log.append(f"could not fit: {layout.pages} pages, {layout.overflow_lines()} line(s) over")
                return FitResult("cannot_fit", cur, tex, layout, compiles, log,
                                 f"could not fit two pages ({layout.pages} pages); needs a manual pass")
            log.append(f"over by {layout.overflow_lines()} line(s) on page 3+; removed: "
                       + "; ".join(describe(c) for c in cuts))
        else:
            cand = _next_addition(cur, layout, failed) if compiles < max_compiles else None
            if cand is None:
                break
            trial = cur.copy()
            apply_cut(trial, cand, add=True)
            trial_tex = render(trial)
            compiles += 1
            trial_pdf = compile_(trial_tex)
            trial_layout = measure(trial_pdf) if trial_pdf else None
            if trial_layout is not None and trial_layout.pages <= 2:
                cur, tex, layout = trial, trial_tex, trial_layout
                log.append(f"added back {describe(cand)}")
            else:
                failed.add(cand.key)
                log.append(f"adding back {describe(cand)} overflowed; kept out")
            continue
        tex = render(cur)
        compiles += 1
        pdf = compile_(tex)
        layout = measure(pdf) if pdf else None
        if layout is None:
            return FitResult("compile_error" if pdf is None else "unmeasured", cur, tex, None, compiles, log)

    if layout.first_words[1:2] == ["•"]:
        log.append("page 2 starts mid-entry: a bullet continues from page 1")
    note = ""
    if any(not k for e in cur.experiences for k in e.keep):
        note = "to fit two pages, a work-experience bullet was removed"
    elif any(before.kept and not now.kept for before, now in zip(plan.projects, cur.projects)):
        note = "to fit two pages, a whole project was removed"
    return FitResult("fit", cur, tex, layout, compiles, log, note)
