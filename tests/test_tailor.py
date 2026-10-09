from pathlib import Path

import pytest

from jobhunter import db, fetch_diag
from jobhunter.models import Job
from jobhunter.tailor import engine, snippet_bank
from jobhunter.tailor.engine import BASE_CV


def _no_llm(monkeypatch):
    """Force the deterministic fallback path -- no real network/CLI call."""
    monkeypatch.setattr(engine.provider, "available", lambda: False)


def _n_experiences(tex: str) -> int:
    # \resumeSubheading is also used by EDUCATION and defined once via \newcommand
    # in the preamble, so counting it document-wide overcounts; scope to the section.
    body = tex.split(r"\section{PROFESSIONAL EXPERIENCE}")[1].split(r"\section{PROJECTS")[0]
    return body.count(r"\resumeSubheading")


def _n_projects(tex: str) -> int:
    # \resumeProjectHeadingFourItemResearch is likewise defined once via \newcommand
    # in the preamble; scope to the section body, not the whole document.
    body = tex.split(r"\section{PROJECTS")[1].split(r"\section{SKILLS}")[0]
    return body.count(r"\resumeProjectHeadingFourItemResearch")


def test_parse_blocks():
    parsed = snippet_bank.parse(BASE_CV)
    # Data Joker is commented out of cv_base.tex (retired, see
    # cv_tailoring_workflow.md) and must not be resurrected by the parser.
    assert len(parsed.projects) == 6
    assert len(parsed.experiences) == 3
    assert len(parsed.skills) == 6
    assert parsed.heading_line
    assert all("Data Joker" not in p.text for p in parsed.projects)
    assert any("Job Hunter" in p.text and len(p.bullets()) == 4 for p in parsed.projects)


def test_terms_boundary_no_false_positive():
    # 'ct' must not match inside 'detection' / 'structural'
    assert "ct" not in snippet_bank.terms_in("object detection and structural work")
    assert "cnn" in snippet_bank.terms_in("we use CNNs heavily")
    assert "fine-tun" in snippet_bank.terms_in("experience with fine-tuning models")


def test_fallback_select_floats_relevant_experience_first():
    parsed = snippet_bank.parse(BASE_CV)
    nlp = Job(source="x", external_id="1", title="LLM Engineer", company="A",
              description="LLM fine-tuning, GRPO, prompt engineering, entity extraction, NLP")
    chosen = engine._fallback_select(parsed.experiences, engine._job_terms(nlp), engine.MAX_EXPERIENCES)
    assert len(chosen) == engine.MAX_EXPERIENCES
    assert any("DiliTrust" in b.text for b in chosen)


def test_fallback_select_caps_and_orders_reverse_chronologically():
    parsed = snippet_bank.parse(BASE_CV)
    # No real relevance signal in a generic description -- every project ties on
    # tag overlap (0), so the cap+date-order behavior is what's under test.
    job = Job(source="x", external_id="1", title="Data Scientist", company="A", description="data")
    chosen = engine._fallback_select(parsed.projects, engine._job_terms(job), engine.MAX_PROJECTS)
    assert len(chosen) == engine.MAX_PROJECTS
    dates = [b.end_date() for b in chosen]
    assert dates == sorted(dates, reverse=True)


def test_fallback_skills_drop_medical_imaging_when_irrelevant(monkeypatch):
    _no_llm(monkeypatch)
    parsed = snippet_bank.parse(BASE_CV)
    job = Job(source="x", external_id="1", title="RAG Engineer", company="A",
              description="RAG, LLM agents, retrieval")
    names = [c.name for c in engine._select_blocks(job, parsed, engine._job_terms(job)).skills]
    assert engine._CONDITIONAL_SKILL_CATEGORY["en"] not in names
    assert "Technical Skills" in names


def test_fallback_skills_keep_medical_imaging_when_relevant(monkeypatch):
    _no_llm(monkeypatch)
    parsed = snippet_bank.parse(BASE_CV)
    job = Job(source="x", external_id="1", title="Medical Imaging Engineer", company="A",
              description="clinical CT and CTA segmentation")
    names = [c.name for c in engine._select_blocks(job, parsed, engine._job_terms(job)).skills]
    assert engine._CONDITIONAL_SKILL_CATEGORY["en"] in names


def test_pick_ignores_out_of_range_and_duplicates_and_respects_order():
    parsed = snippet_bank.parse(BASE_CV)
    chosen = engine._pick(parsed.projects, [2, 99, 2, "x", 0], [[10, 20], [], [], [], [30]], cap=5)
    assert chosen == [(parsed.projects[2], [10, 20]), (parsed.projects[0], [30])]   # scores follow their id's position


def test_pick_non_list_returns_empty():
    parsed = snippet_bank.parse(BASE_CV)
    assert engine._pick(parsed.projects, None, None, cap=3) == []


def test_pick_drops_non_numeric_scores_and_caps():
    parsed = snippet_bank.parse(BASE_CV)
    chosen = engine._pick(parsed.projects, [0, 1, 2], [[5, "x", None, 7]], cap=2)
    assert chosen[0][1] == [5, 7] and len(chosen) == 2


def test_block_bullets_counts_real_bullets_and_is_empty_for_description_only_project():
    parsed = snippet_bank.parse(BASE_CV)
    dilitrust = next(b for b in parsed.experiences if "DiliTrust" in b.text)
    assert len(dilitrust.bullets()) == 4
    nerf = next(b for b in parsed.projects if "NeRF" in b.text)
    assert nerf.bullets() == []  # description-only, no \resumeItemListStart at all


def test_filter_bullets_keeps_only_chosen_indices_in_original_order():
    parsed = snippet_bank.parse(BASE_CV)
    dilitrust = next(b for b in parsed.experiences if "DiliTrust" in b.text)
    filtered = snippet_bank.filter_bullets(dilitrust.text, [2, 0])
    kept = snippet_bank.Block(filtered).bullets()
    original = dilitrust.bullets()
    assert kept == [original[0], original[2]]   # original order preserved, not the given order


def test_filter_bullets_falls_back_to_full_text_when_keep_is_empty():
    parsed = snippet_bank.parse(BASE_CV)
    dilitrust = next(b for b in parsed.experiences if "DiliTrust" in b.text)
    assert snippet_bank.filter_bullets(dilitrust.text, []) == dilitrust.text


def test_filter_bullets_no_op_on_description_only_project():
    parsed = snippet_bank.parse(BASE_CV)
    nerf = next(b for b in parsed.projects if "NeRF" in b.text)
    assert snippet_bank.filter_bullets(nerf.text, [0, 1]) == nerf.text


def test_a_trimmed_plan_renders_only_its_kept_bullets_items_and_modules(monkeypatch):
    _no_llm(monkeypatch)
    parsed = snippet_bank.parse(BASE_CV)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    draft = engine._draft(job, parsed)
    full = draft.render()
    plan = draft.plan.copy()
    plan.experiences[0].keep[0] = False
    plan.skills[0].keep[0] = False
    plan.modules = 2
    tex = draft.render(plan)
    first_exp = draft.selection.experiences[0].bullets()
    assert first_exp[0] in full and first_exp[0] not in tex and first_exp[1] in tex
    assert "Python," in full.split(r"\section{SKILLS}")[1] and "Python," not in tex.split(r"\section{SKILLS}")[1]
    assert len(tex) < len(full)


def test_tailor_uses_llm_selection_when_available(monkeypatch):
    """The primary path: an LLM call (mirroring cv_tailoring_workflow.md) chooses
    which real blocks to keep, in the order it gives -- not re-sorted by date."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    parsed = snippet_bank.parse(BASE_CV)
    captured = {}

    def fake_select(job, experiences, projects, skills, judge_context=None):
        captured["n_experiences"] = len(experiences)
        captured["n_projects"] = len(projects)
        captured["skill_names"] = [name for name, _ in skills]
        return {
            "experience_ids": [1, 0],           # deliberately not date order
            "experience_scores": [[], []],
            "project_ids": [3, 1],
            "project_scores": [[], []],
            "skill_scores": [],
            "reasoning": "test",
        }

    monkeypatch.setattr(engine.llm_select, "select", fake_select)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job, parsed=parsed)

    assert captured["n_experiences"] == len(parsed.experiences)
    assert captured["n_projects"] == len(parsed.projects)
    assert "Languages" not in captured["skill_names"] and "Technical Skills" in captured["skill_names"]
    # experience_ids [1, 0] means block 1 appears before block 0 in the output
    assert tex.index(parsed.experiences[1].text.strip()[:40]) < tex.index(parsed.experiences[0].text.strip()[:40])
    assert _n_projects(tex) == 2
    assert "Languages" in tex.split(r"\section{SKILLS}")[1]      # every category is kept for the fit step


def test_llm_scores_stay_with_their_entries_and_items(monkeypatch):
    """The LLM only ranks: its scores become the plan the fit step trims by."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    parsed = snippet_bank.parse(BASE_CV)
    dilitrust_idx = next(i for i, b in enumerate(parsed.experiences) if "DiliTrust" in b.text)
    n_items = len(snippet_bank.split_skill_items(parsed.skills[0].line).items)

    monkeypatch.setattr(engine.llm_select, "select", lambda job, e, p, s, judge_context=None: {
        "experience_ids": [dilitrust_idx], "experience_scores": [[90, 10, 70, 30]],
        "project_ids": [0], "project_scores": [[]],
        "skill_scores": [list(range(n_items))],
        "reasoning": "test",
    })
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    plan = engine._draft(job, parsed).plan

    assert plan.experiences[0].scores == [90, 10, 70, 30]
    assert plan.skills[0].scores == list(range(n_items)) and plan.skills[0].trimmable
    assert not plan.skills[-1].trimmable                                   # the Languages line is never trimmed
    assert all(plan.experiences[0].keep)                                   # nothing is cut before measuring


def test_missing_scores_default_to_neutral(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
        "experience_ids": [0], "experience_scores": [[80]],
        "project_ids": [0], "project_scores": [], "skill_scores": [], "reasoning": ""})
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    plan = engine._draft(job, snippet_bank.parse(BASE_CV)).plan
    assert plan.experiences[0].scores[0] == 80 and set(plan.experiences[0].scores[1:]) == {50}
    assert set(plan.skills[0].scores) == {50}


def test_tailor_falls_back_when_llm_selection_raises(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("cli exploded")

    monkeypatch.setattr(engine.llm_select, "select", boom)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job)  # must not raise
    assert _n_projects(tex) == engine.MAX_PROJECTS


def test_tailor_falls_back_when_llm_selection_is_incomplete(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
        "experience_ids": [], "experience_scores": [],
        "project_ids": [0], "project_scores": [[]],
        "skill_scores": [], "reasoning": "",
    })
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job)  # incomplete result -> deterministic fallback, not a half-empty CV
    assert _n_experiences(tex) == engine.MAX_EXPERIENCES


def test_tailor_uses_fixed_generic_tagline_and_is_valid_latex_structure(monkeypatch):
    # Tagline is a fixed generic line, no per-job "targeting <role> at <company>"
    # clause -- see templates/cv_tailoring_workflow.md's header-tagline rule.
    _no_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer & Data H/F",
              company="Acme & Co", description="machine learning")
    tex = engine.tailor_tex(job)
    assert engine._tagline() in tex
    assert "targeting" not in tex
    assert tex.count(r"\begin{document}") == 1
    assert tex.count(r"\end{document}") == 1


def test_tailor_never_exceeds_caps(monkeypatch):
    _no_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job)
    assert _n_experiences(tex) == engine.MAX_EXPERIENCES
    assert _n_projects(tex) == engine.MAX_PROJECTS


class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_compile_missing_latexmk_degrades_gracefully(tmp_path, monkeypatch):
    # No latexmk binary on PATH -> compile returns None, .tex kept, log explains why.
    def _missing(*a, **k):
        raise FileNotFoundError("latexmk")
    monkeypatch.setattr(engine.subprocess, "run", _missing)
    out = engine.compile_tex(r"\begin{document}hi\end{document}", tmp_path)
    assert out is None
    assert (tmp_path / "cv.tex").exists()
    log = (tmp_path / "cv.compile.log").read_text()
    assert "latexmk" in log


def test_compile_accepts_matching_page_count(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, env):
        (Path(cwd) / "cv.pdf").write_bytes(b"%PDF-fake")
        return _FakeProc(0, "Output written on cv.pdf (2 pages, 123 bytes).\n")
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    out = engine.compile_tex(r"\begin{document}hi\end{document}", tmp_path, expected_pages=2)
    assert out == tmp_path / "cv.pdf"


def test_compile_rejects_wrong_page_count_when_expected_pages_given(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, env):
        (Path(cwd) / "cv.pdf").write_bytes(b"%PDF-fake")
        return _FakeProc(0, "Output written on cv.pdf (3 pages, 123 bytes).\n")
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    out = engine.compile_tex(r"\begin{document}hi\end{document}", tmp_path, expected_pages=2)
    assert out is None
    assert (tmp_path / "cv.pdf").exists()   # kept on disk for manual review
    log = (tmp_path / "cv.compile.log").read_text()
    assert "3" in log and "expected exactly 2" in log


def test_compile_without_expected_pages_ignores_page_count(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, env):
        (Path(cwd) / "cv.pdf").write_bytes(b"%PDF-fake")
        return _FakeProc(0, "Output written on cv.pdf (5 pages, 123 bytes).\n")
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    out = engine.compile_tex(r"\begin{document}hi\end{document}", tmp_path)
    assert out == tmp_path / "cv.pdf"


def test_tailor_job_auto_true_requests_two_page_gate(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "compile_tex", lambda tex, out_dir, name="cv", expected_pages=None:
                        captured.update(expected_pages=expected_pages) or None)
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    _no_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    engine.tailor_job(job, 1, auto=True)
    assert captured["expected_pages"] == 2


def test_tailor_job_auto_false_default_skips_page_gate(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(engine, "compile_tex", lambda tex, out_dir, name="cv", expected_pages=None:
                        captured.update(expected_pages=expected_pages) or None)
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    _no_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    engine.tailor_job(job, 1)
    assert captured["expected_pages"] is None


def test_page_layout_is_none_without_pdftotext(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_pdftotext_path", lambda: None)
    assert engine._page_layout(tmp_path / "cv.pdf") is None


def test_page_layout_is_none_when_pdftotext_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_pdftotext_path", lambda: "/usr/bin/pdftotext")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: _FakeProc(1, ""))
    assert engine._page_layout(tmp_path / "cv.pdf") is None


def test_page_layout_parses_pdftotext_bbox_output(tmp_path, monkeypatch):
    out = ('<doc><page width="612.0" height="792.0">'
           '<word xMin="50" yMin="40" xMax="90" yMax="50">Name</word>'
           '<word xMin="50" yMin="53" xMax="90" yMax="63">Next</word></page></doc>')
    monkeypatch.setattr(engine, "_pdftotext_path", lambda: "/usr/bin/pdftotext")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: _FakeProc(0, out))
    layout = engine._page_layout(tmp_path / "cv.pdf")
    assert layout.pages == 1 and layout.line_counts == [2] and layout.bottoms == [63.0]


def _counting_llm(monkeypatch):
    """LLM selection and summary succeed and count how often they are asked."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    calls = {"select": 0, "summary": 0}

    def select(job, e, p, s, judge_context=None):
        calls["select"] += 1
        return {"experience_ids": [0, 1], "experience_scores": [[90, 40, 70, 60], [50, 50, 50]],
                "project_ids": [0, 1, 2], "project_scores": [[], [], []],
                "extra_project_ids": [3], "extra_project_scores": [[]],
                "skill_scores": [], "reasoning": "r"}

    def summary_text(*a, **k):
        calls["summary"] += 1
        return ("Machine Learning Engineer with hands-on LLM engineering experience from a recent internship "
                "in Paris, building agentic pipelines and evaluation tooling. Applying this to the ML role at Acme.")

    monkeypatch.setattr(engine.llm_select, "select", select)
    monkeypatch.setattr(engine.summary, "generate_summary", summary_text)
    return calls


def test_a_tailoring_saves_its_llm_answer_and_summary_and_a_refit_reuses_them(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    calls = _counting_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    first = engine.tailor_job(job, 1, auto=True, role_category="AI")
    plan = engine.load_plan(first.tex_path)

    asked = dict(calls)
    assert asked["select"] == 1 and asked["summary"] >= 1
    assert plan["language"] == "en" and plan["role_category"] == "AI"
    assert plan["selection"]["experience_scores"][0] == [90, 40, 70, 60]
    saved_summary = plan["summary"]["text"]
    assert saved_summary and saved_summary in first.tex_path.read_text()

    again = engine.tailor_job(job, 1, auto=True, role_category="AI", language=plan["language"], stored=plan)

    assert calls == asked                                           # no new LLM call at all
    assert again.tex_path != first.tex_path and again.pdf_path is not None
    assert saved_summary in again.tex_path.read_text()               # the same summary is in the new CV


def test_a_cv_made_by_the_keyword_fallback_has_no_plan_to_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    _no_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    result = engine.tailor_job(job, 1, auto=True)

    assert result.fallback and engine.load_plan(result.tex_path) is None
    assert engine.load_plan(tmp_path / "missing.tex") is None


def _llm_ok(monkeypatch):
    """LLM block selection succeeds with a minimal valid pick, so no keyword fallback."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    monkeypatch.setattr(engine.llm_select, "select", lambda job, e, p, s, judge_context=None: {
        "experience_ids": [0], "experience_scores": [[]], "project_ids": [0], "project_scores": [[]],
        "skill_scores": [], "reasoning": ""})


def _two_page_layout(monkeypatch, free_lines=1.0):
    """Pretend every compile measured as a full two-page CV (so no trimming or adding back)."""
    layout = engine.fit.Layout(2, 792.0, 12.0, [756 - 36 - free_lines * 12 + 36] * 2, [55, 55], ["Name", "PROJECTS"])
    monkeypatch.setattr(engine, "_page_layout", lambda pdf: layout)


def _tailor_with_tracking(tmp_path, monkeypatch, compile_fn, auto=True):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    _llm_ok(monkeypatch)
    _two_page_layout(monkeypatch)
    monkeypatch.setattr(engine, "compile_tex", compile_fn)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    with fetch_diag.run_tracking() as tracker:
        result = engine.tailor_job(job, 1, auto=auto)
    return result, tracker


def _failing_compile(log_text):
    def fake(tex, out_dir, name="cv", expected_pages=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.tex").write_text(tex, encoding="utf-8")
        (out_dir / f"{name}.compile.log").write_text(log_text, encoding="utf-8")
        return None
    return fake


def test_tailor_job_records_a_page_count_failure(tmp_path, monkeypatch):
    log = "Compiled to 3 page(s), expected exactly 2. PDF kept at x for review.\n"
    result, tracker = _tailor_with_tracking(tmp_path, monkeypatch, _failing_compile(log))

    assert result.pdf_path is None
    assert "3 page(s)" in result.note
    assert dict(tracker.counts) == {("tailor", "Acme", "tailor_page_count"): 1}   # final outcome only


def test_tailor_job_records_a_latex_error(tmp_path, monkeypatch):
    result, tracker = _tailor_with_tracking(tmp_path, monkeypatch, _failing_compile("! Undefined control sequence.\n"))

    assert result.pdf_path is None and "LaTeX compile error" in result.note
    assert dict(tracker.counts) == {("tailor", "Acme", "tailor_latex_error"): 1}


def test_tailor_job_records_failures_for_the_interactive_path_too(tmp_path, monkeypatch):
    result, tracker = _tailor_with_tracking(tmp_path, monkeypatch, _failing_compile("! boom\n"), auto=False)

    assert result.note and ("tailor", "Acme", "tailor_latex_error") in tracker.counts


def _writing_compile(tex, out_dir, name="cv", expected_pages=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.tex").write_text(tex, encoding="utf-8")
    (out_dir / f"{name}.pdf").write_bytes(b"%PDF " + name.encode())
    return out_dir / f"{name}.pdf"


def test_each_tailoring_gets_its_own_files_and_cv_tex_pdf_mirror_the_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    _no_llm(monkeypatch)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    stamps = iter(["20260101-000000-000", "20260102-000000-000"])
    monkeypatch.setattr(engine, "_version_stamp", lambda: next(stamps))
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    first = engine.tailor_job(job, 1, auto=True)
    second = engine.tailor_job(job, 1, auto=True)

    folder = tmp_path / "1-acme"
    assert first.tex_path == folder / "cv-20260101-000000-000.tex"
    assert second.pdf_path == folder / "cv-20260102-000000-000.pdf"
    assert first.pdf_path.read_bytes() == b"%PDF cv-20260101-000000-000"   # the earlier version survives
    assert (folder / "cv.pdf").read_bytes() == second.pdf_path.read_bytes()   # latest working copy
    assert (folder / "cv.tex").read_text() == second.tex_path.read_text()


def test_cv_compile_log_mirrors_a_failure_and_is_cleared_by_the_next_success(tmp_path, monkeypatch):
    log = "Compiled to 3 page(s), expected exactly 2.\n"
    _tailor_with_tracking(tmp_path, monkeypatch, _failing_compile(log))
    assert "Compiled to 3" in (tmp_path / "1-acme" / "cv.compile.log").read_text()

    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    engine.tailor_job(job, 1, auto=True)

    assert not (tmp_path / "1-acme" / "cv.compile.log").exists()   # no stale "needs a manual pass" evidence


@pytest.mark.parametrize("setup,reason", [
    ("unavailable", "tailor_llm_unavailable"),
    ("raises", "tailor_llm_error"),
    ("incomplete", "tailor_llm_unusable"),
])
def test_keyword_fallback_is_tracked_and_marked_on_the_cv(tmp_path, monkeypatch, setup, reason):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    monkeypatch.setattr(engine.provider, "available", lambda: setup != "unavailable")
    if setup == "raises":
        def boom(*a, **k):
            raise RuntimeError("usage limit reached")
        monkeypatch.setattr(engine.llm_select, "select", boom)
    elif setup == "incomplete":
        monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
            "experience_ids": [], "experience_scores": [], "project_ids": [0],
            "project_scores": [[]], "skill_scores": [], "reasoning": ""})
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    with fetch_diag.run_tracking() as tracker:
        result = engine.tailor_job(job, 1, auto=True)

    assert result.fallback and result.note == db.CV_FALLBACK_NOTE
    assert result.pdf_path is not None                       # still a usable CV
    assert dict(tracker.counts) == {("tailor", "Acme", reason): 1}


def test_fallback_marker_is_kept_alongside_a_compile_failure_note(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    _no_llm(monkeypatch)
    monkeypatch.setattr(engine, "compile_tex", _failing_compile("! boom\n"))
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    result = engine.tailor_job(job, 1, auto=True)

    assert result.fallback and "LaTeX compile error" in result.note


def test_llm_selection_success_is_not_marked_as_fallback(tmp_path, monkeypatch):
    result, tracker = _tailor_with_tracking(tmp_path, monkeypatch, _writing_compile)
    assert not result.fallback and dict(tracker.counts) == {}


# ------------------------------------------------------------------ general CVs (no posting, no summary)

def test_a_general_cv_uses_the_brief_as_the_posting_and_writes_no_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    calls = _counting_llm(monkeypatch)
    seen, inner = [], engine.llm_select.select
    monkeypatch.setattr(engine.llm_select, "select", lambda job, *a, **k: seen.append(job) or inner(job, *a, **k))

    result = engine.tailor_general("llm", "A general CV for LLM engineer roles.", "en")

    assert calls["select"] == 1 and calls["summary"] == 0               # no summary is ever written
    assert seen[0].description == "A general CV for LLM engineer roles." and seen[0].source == "general"
    assert result.tex_path.parent == tmp_path / "general-llm-en"
    tex = result.tex_path.read_text()
    assert "%SUMMARY-BEGIN\n%SUMMARY-END" in tex                          # an empty summary block
    assert "Seeking a Machine Learning role" in tex                       # the standard header tagline
    assert engine.load_plan(result.tex_path)["summary"]["text"] == ""     # so a refit stays summary-free


def test_a_general_cv_in_french_uses_the_french_master_and_its_own_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    _no_llm(monkeypatch)

    result = engine.tailor_general("cv", "Un CV général.", "fr")

    assert result.lang == "fr" and result.tex_path.parent == tmp_path / "general-cv-fr"
    assert "Recherche" in result.tex_path.read_text() or "recherche" in result.tex_path.read_text()


def test_a_normal_tailoring_still_writes_a_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    calls = _counting_llm(monkeypatch)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")

    engine.tailor_job(job, 1, auto=True)

    assert calls["summary"] >= 1


def test_the_general_briefs_file_has_the_llm_speech_and_cv_presets_and_their_pins_resolve_in_both_masters():
    briefs = engine.load_general_briefs()
    assert {"llm", "llm-speech", "cv"} <= set(briefs) and all(len(p["brief"]) > 100 for p in briefs.values())
    for lang in ("en", "fr"):
        parsed = snippet_bank.parse(engine.base_cv_path(lang), lang)
        for preset in briefs.values():
            pins = {k: preset[k] for k in ("experiences", "projects") if preset.get(k)}
            ids = engine._resolve_pins(parsed, pins)                    # raises if a name is missing or ambiguous
            assert all(len(set(v)) == len(v) for v in ids.values())


def test_a_plain_text_preset_is_a_brief_without_pins(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text("a: just text\nb:\n  brief: more\n  experiences: [X]\nc: ''\n", encoding="utf-8")
    assert engine.load_general_briefs(path) == {"a": {"brief": "just text"}, "b": {"brief": "more", "experiences": ["X"]}}


def test_a_pinned_name_must_match_exactly_one_entry_and_pins_respect_the_caps():
    parsed = snippet_bank.parse(BASE_CV, "en")
    assert engine._resolve_pins(parsed, {"experiences": ["deepwise", "DILITRUST"]}) == {"experiences": [1, 0]}
    with pytest.raises(ValueError, match="matches 0"):
        engine._resolve_pins(parsed, {"projects": ["no such project"]})
    with pytest.raises(ValueError, match="matches 3"):
        engine._resolve_pins(parsed, {"experiences": ["Intern"]})              # all three internships say "Intern"
    with pytest.raises(ValueError, match="at most"):
        engine._resolve_pins(parsed, {"experiences": ["DiliTrust", "DeepWise", "Orange Labs"]})


def test_the_pinned_answer_replaces_the_picks_and_keeps_the_llms_scores_where_it_gave_them():
    parsed = snippet_bank.parse(BASE_CV, "en")
    answer = {"experience_ids": [0, 1], "experience_scores": [[90, 80, 70, 60], [50, 50, 50]],
              "project_ids": [1, 2], "project_scores": [[10, 20], []],
              "extra_project_ids": [0], "extra_project_scores": [[5, 6, 7, 8]], "skill_scores": [], "reasoning": "r"}
    out = engine._pinned_answer(answer, {"experiences": [0, 2], "projects": [0, 4]}, parsed)
    assert out["experience_ids"] == [0, 2] and out["experience_scores"][0] == [90, 80, 70, 60]
    assert out["experience_scores"][1] == [50, 50, 50]                       # Orange Labs was not scored: flat 50
    assert out["project_ids"] == [0, 4] and out["project_scores"][0] == [5, 6, 7, 8]    # taken from the extras
    assert out["extra_project_ids"] == [] and out["skill_scores"] == []


def test_pins_fix_the_internships_shown_and_the_select_call_is_told_which(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    _counting_llm(monkeypatch)                                              # its answer picks experiences 0 and 1
    told, inner = [], engine.llm_select.select
    monkeypatch.setattr(engine.llm_select, "select", lambda job, *a, pinned=None, **k: told.append(pinned) or inner(job, *a, **k))

    result = engine.tailor_general("llm-speech", "brief text", "en", pins={"experiences": ["DiliTrust", "Orange Labs"]})

    tex = result.tex_path.read_text()
    experience = tex.split(r"\section{PROFESSIONAL EXPERIENCE}")[1].split(r"\section{PROJECTS")[0]
    assert "Orange Labs" in experience and "DeepWise" not in experience
    assert told == [{"experiences": [0, 2]}]
    assert engine.load_plan(result.tex_path)["selection"]["experience_ids"] == [0, 2]    # a refit keeps the pins


def test_without_pins_the_select_call_gets_no_pinned_argument(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _writing_compile)
    _two_page_layout(monkeypatch)
    _counting_llm(monkeypatch)
    told, inner = [], engine.llm_select.select
    monkeypatch.setattr(engine.llm_select, "select", lambda job, *a, **k: told.append(dict(k)) or inner(job, *a, **k))

    engine.tailor_general("cv", "brief text", "en")

    assert told and all("pinned" not in k for k in told)


def test_the_tailor_general_command_rejects_an_unknown_preset_and_a_nameless_custom_brief(capsys):
    from jobhunter import cli
    assert cli.main(["tailor-general", "nope"]) == 1
    assert "no such preset" in capsys.readouterr().out
    assert cli.main(["tailor-general", "--brief", "x"]) == 1
    assert "--name is required" in capsys.readouterr().out
