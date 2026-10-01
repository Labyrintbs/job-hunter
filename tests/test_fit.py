"""The measured fit to two pages (tailor/fit.py) and the skills-line item parsing it relies on."""
import math
from pathlib import Path

import pytest

from jobhunter.tailor import engine, fit, snippet_bank

BBOX = """<doc>
<page width="612.000000" height="792.000000">
<word xMin="50" yMin="40.0" xMax="90" yMax="52.0">HONGMING</word>
<word xMin="95" yMin="40.2" xMax="130" yMax="52.0">FANG</word>
<word xMin="50" yMin="65.0" xMax="90" yMax="77.0">Paris</word>
<word xMin="50" yMin="90.0" xMax="90" yMax="102.0">Next</word>
<word xMin="50" yMin="700.0" xMax="90" yMax="712.0">Last</word>
</page>
<page width="612.000000" height="792.000000">
<word xMin="40" yMin="40.0" xMax="50" yMax="52.0">•</word>
<word xMin="55" yMin="40.0" xMax="90" yMax="52.0">continued</word>
<word xMin="50" yMin="60.0" xMax="90" yMax="72.0">more</word>
</page>
</doc>"""


def test_parse_bbox_reads_pages_lines_bottoms_and_the_first_word():
    lay = fit.parse_bbox(BBOX)
    assert lay.pages == 2 and lay.line_counts == [4, 2]
    assert lay.bottoms == [712.0, 72.0] and lay.first_words == ["HONGMING", "•"]
    assert lay.pitch == 20.0                           # gaps over 20pt (section spacing) don't count as line pitch
    assert lay.limit == 792 - fit.BOTTOM_MARGIN_PT


def test_parse_bbox_is_none_for_empty_or_pageless_output():
    assert fit.parse_bbox("") is None and fit.parse_bbox(None) is None and fit.parse_bbox("<doc></doc>") is None


def test_layout_free_and_overflow_lines():
    lay = fit.Layout(3, 792.0, 12.0, [750.0, 700.0, 100.0], [50, 50, 4], ["a", "b", "c"])
    assert lay.overflow_lines() == 4
    assert lay.free_lines(1) == pytest.approx((756 - 700) / 12)
    assert lay.free_lines(5) == 0.0
    assert lay.free_lines(0) == pytest.approx(0.5)


# ----------------------------------------------------------------- skills line items

def test_every_skills_line_of_both_masters_round_trips_and_has_no_empty_item():
    for lang, path in (("en", engine.BASE_CV), ("fr", engine.BASE_CV_FR)):
        for cat in snippet_bank.parse(path, lang).skills:
            parts = snippet_bank.split_skill_items(cat.line)
            assert all(i.strip() for i in parts.items), cat.name
            assert snippet_bank.join_skill_items(parts, [True] * len(parts.items)) == cat.line


def test_split_does_not_break_inside_parentheses_and_keeps_group_separators():
    parts = snippet_bank.split_skill_items(
        r"\textbf{Tech:} Python, C/C++; PyTorch, NumPy (a, b); Docker")
    assert parts.items == ["Python", "C/C++", "PyTorch", "NumPy (a, b)", "Docker"]
    assert parts.seps == ["", ", ", "; ", ", ", "; "]


def test_dropping_a_group_head_moves_its_semicolon_to_the_next_item():
    parts = snippet_bank.split_skill_items(r"\textbf{Tech:} Python, C/C++; PyTorch, NumPy; Docker")
    line = snippet_bank.join_skill_items(parts, [True, True, False, True, True])
    assert line == r"\textbf{Tech:} Python, C/C++; NumPy; Docker"
    line = snippet_bank.join_skill_items(parts, [False, True, True, True, True])
    assert line == r"\textbf{Tech:} C/C++; PyTorch, NumPy; Docker"
    assert snippet_bank.join_skill_items(parts, [True, True, False, False, True]) == r"\textbf{Tech:} Python, C/C++; Docker"


def test_french_spaced_semicolons_are_preserved():
    parts = snippet_bank.split_skill_items(r"\textbf{Techniques :} Python, C/C++ ; PyTorch")
    assert parts.seps == ["", ", ", " ; "]
    assert snippet_bank.join_skill_items(parts, [True] * 3).endswith("C/C++ ; PyTorch")


# --------------------------------------------------------------------- cut ordering

def _plan(exp=(3, 3), projects=(3, 3, 3), skill_items=6, scores=None):
    def entry(n, base=3.0):
        e = fit.Entry([2.0] * n, list(scores or [50] * n), [True] * n, base)
        return e
    skills = [fit.SkillLine([20] * skill_items, list(range(10, 10 + skill_items * 10, 10)), [True] * skill_items),
              fit.SkillLine([20] * 3, [50] * 3, [True] * 3, trimmable=False)]
    return fit.Plan([entry(n) for n in exp], [entry(n) for n in projects], skills)


def test_modules_go_first_then_low_scored_skills_then_project_bullets_then_work_last():
    cuts = fit.removals(_plan())
    assert cuts[0].kind == "module"
    kinds = [c.kind for c in cuts]
    assert kinds.index("skill") < kinds.index("project_bullet") < kinds.index("exp_bullet")
    skills = [c for c in cuts if c.kind == "skill"]
    assert [c.eff for c in skills] == sorted(c.eff for c in skills)        # lowest score first
    assert min(c.eff for c in cuts if c.kind == "exp_bullet") >= 100       # work experience is the last resort


def test_a_high_scored_skill_outlasts_a_low_scored_project_bullet():
    plan = _plan(scores=[5, 5, 5])
    plan.skills[0].scores = [95] * 6
    kinds = [c.kind for c in fit.removals(plan)]
    assert kinds.index("project_bullet") < kinds.index("skill") or all(
        c.eff >= 95 for c in fit.removals(plan) if c.kind == "skill")
    cheapest_bullet = min(c.eff for c in fit.removals(plan) if c.kind == "project_bullet")
    assert cheapest_bullet == 25 and cheapest_bullet < 95


def test_the_minimums_are_never_cut_below():
    plan = _plan(exp=(2, 2), projects=(1, 1), skill_items=2)
    assert plan.modules > fit.MIN_MODULES
    plan.modules = fit.MIN_MODULES
    assert fit.removals(plan) == []                    # 2 bullets per entry, 1 per project, 2 items, 2 modules, 2 projects


def test_a_whole_project_is_removable_only_above_the_project_minimum():
    plan = _plan(exp=(2, 2), projects=(1, 1, 1), skill_items=2)
    plan.modules = fit.MIN_MODULES
    assert [c.kind for c in fit.removals(plan)] == ["project"] * 3
    plan.projects[0].kept = False
    assert fit.removals(plan) == []                    # two projects left


def test_fixed_skills_lines_are_never_cut():
    plan = _plan()
    assert all(c.a != 1 for c in fit.removals(plan) if c.kind == "skill")


def test_additions_are_the_reverse_of_removals_best_first():
    plan = _plan()
    cuts = fit.removals(plan)[:6]
    for c in cuts:
        fit.apply_cut(plan, c)
    back = fit.additions(plan)
    assert {c.key for c in back} == {c.key for c in cuts}
    assert [c.eff for c in back] == sorted((c.eff for c in back), reverse=True)
    for c in back:
        fit.apply_cut(plan, c, add=True)
    assert fit.removals(plan) == fit.removals(_plan())


# ------------------------------------------------------------------------- the loop

CAPACITY = 60.0


class FakeCV:
    """Render, compile and measure stand-ins: the 'layout' comes from the plan's estimated size."""
    def __init__(self, capacity=CAPACITY):
        self.capacity, self.plans, self.compiled = capacity, {}, []

    def size(self, plan):
        total = plan.modules * fit.MODULE_LINES
        for e in plan.experiences + [p for p in plan.projects if p.kept]:
            total += e.base_lines + sum(l for l, k in zip(e.lines, e.keep) if k)
        for s in plan.skills:
            total += sum(c for c, k in zip(s.chars, s.keep) if k) / fit.SKILL_CHARS_PER_LINE
        return total

    def render(self, plan):
        tex = f"T{len(self.plans)}"
        self.plans[tex] = plan.copy()
        return tex

    def compile(self, tex):
        self.compiled.append(tex)
        return Path(tex)

    def measure(self, pdf):
        total = self.size(self.plans[pdf.name])
        if total > self.capacity:
            return fit.Layout(3, 792.0, 12.0, [744.0, 744.0, 100.0], [50, 50, math.ceil(total - self.capacity)],
                              ["a", "b", "c"])
        free = self.capacity - total
        return fit.Layout(2, 792.0, 12.0, [744.0, 756 - free * 12], [50, 50], ["a", "b"])


def _run(plan, capacity=CAPACITY, **kw):
    cv = FakeCV(capacity)
    return fit.fit(plan, cv.render, cv.compile, cv.measure, **kw), cv


def test_a_cv_that_already_fits_is_left_whole_after_one_compile():
    plan = _plan()
    res, cv = _run(plan, capacity=FakeCV().size(plan) + 1.0)
    assert res.status == "fit" and res.compiles == 1 and res.note == ""
    assert fit.removals(res.plan) == fit.removals(plan)       # nothing removed


def test_an_overflowing_cv_loses_its_lowest_scored_items_first_and_then_fits():
    plan = _plan()
    full = FakeCV().size(plan)
    res, cv = _run(plan, capacity=full - 9)
    assert res.status == "fit" and res.layout.pages == 2
    removed = [c for c in fit.additions(res.plan)]
    assert {c.kind for c in removed} <= {"module", "skill", "project_bullet"}      # never work experience
    assert all(e.kept_count() == len(e.keep) for e in res.plan.experiences)
    assert res.note == "" and any("removed:" in line for line in res.log)
    assert cv.size(res.plan) <= cv.capacity


def test_leftover_room_is_filled_back_with_the_best_removed_items():
    plan = _plan()
    full = FakeCV().size(plan)
    res, cv = _run(plan, capacity=full - 3)
    free = cv.capacity - cv.size(res.plan)
    candidates = [c for c in fit.additions(res.plan) if c.lines <= free]
    assert not candidates or free < fit.ADD_BACK_MIN_FREE_LINES        # nothing that fits is left out


def test_an_addition_that_overflows_is_undone_and_not_retried():
    plan = _plan()
    full = FakeCV().size(plan)
    res, cv = _run(plan, capacity=full - 6)
    assert res.status == "fit" and cv.size(res.plan) <= cv.capacity
    assert len(set(cv.compiled)) == len(cv.compiled) == res.compiles <= fit.MAX_COMPILES


def test_work_experience_is_cut_only_when_nothing_cheaper_is_left_and_is_flagged():
    plan = _plan(exp=(4, 4), projects=(1, 1), skill_items=2)
    plan.modules = fit.MIN_MODULES
    full = FakeCV().size(plan)
    res, cv = _run(plan, capacity=full - 3)
    assert res.status == "fit" and "work-experience bullet was removed" in res.note
    assert all(e.kept_count() >= fit.MIN_EXP_BULLETS for e in res.plan.experiences)


def test_a_whole_project_removal_is_flagged_but_routine_trimming_is_not():
    plan = _plan(exp=(2, 2), projects=(2, 2, 2), skill_items=2)
    plan.modules = fit.MIN_MODULES
    for p in plan.projects:
        p.keep[1] = False                     # one bullet each: only whole projects can still go
    res, _ = _run(plan, capacity=FakeCV().size(plan) - 4)
    assert res.status == "fit" and res.note == "to fit two pages, a whole project was removed"


def test_it_gives_up_with_a_note_when_nothing_is_left_to_remove():
    plan = _plan(exp=(2, 2), projects=(1, 1), skill_items=2)
    plan.modules = fit.MIN_MODULES
    res, _ = _run(plan, capacity=5)
    assert res.status == "cannot_fit" and "could not fit two pages" in res.note


def test_the_compile_budget_is_respected():
    plan = _plan(exp=(5, 5), projects=(5, 5, 5), skill_items=10)
    res, cv = _run(plan, capacity=FakeCV().size(plan) * 0.4, max_compiles=1)
    assert len(cv.compiled) == 1 and res.status == "cannot_fit"


def test_a_compile_error_or_an_unreadable_layout_is_reported_not_guessed():
    plan = _plan()
    broken = fit.fit(plan, lambda p: "x", lambda t: None, lambda pdf: None)
    unreadable = fit.fit(plan, lambda p: "x", lambda t: Path("x"), lambda pdf: None)
    assert broken.status == "compile_error" and unreadable.status == "unmeasured"


def test_a_page_two_that_starts_with_a_bullet_is_logged_as_a_split_entry():
    plan = _plan()
    lay = fit.Layout(2, 792.0, 12.0, [744.0, 744.0], [50, 50], ["Name", "•"])
    res = fit.fit(plan, lambda p: "x", lambda t: Path("x"), lambda pdf: lay)
    assert res.status == "fit" and any("starts mid-entry" in line for line in res.log)


def test_the_input_plan_is_not_modified():
    plan = _plan()
    before = fit.removals(plan)
    _run(plan, capacity=FakeCV().size(plan) - 10)
    assert fit.removals(plan) == before
