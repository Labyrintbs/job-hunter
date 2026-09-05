from pathlib import Path

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
    assert len(parsed.projects) == 5
    assert len(parsed.experiences) == 3
    assert len(parsed.skills) == 6
    assert parsed.heading_line
    assert all("Data Joker" not in p.text for p in parsed.projects)


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


def test_fallback_select_skills_drops_medical_imaging_when_irrelevant():
    parsed = snippet_bank.parse(BASE_CV)
    job = Job(source="x", external_id="1", title="RAG Engineer", company="A",
              description="RAG, LLM agents, retrieval")
    kept = engine._fallback_select_skills(parsed.skills, engine._job_terms(job))
    names = [c.name for c in kept]
    assert engine._CONDITIONAL_SKILL_CATEGORY not in names
    assert "Technical Skills" in names


def test_fallback_select_skills_keeps_medical_imaging_when_relevant():
    parsed = snippet_bank.parse(BASE_CV)
    job = Job(source="x", external_id="1", title="Medical Imaging Engineer", company="A",
              description="clinical CT and CTA segmentation")
    kept = engine._fallback_select_skills(parsed.skills, engine._job_terms(job))
    assert engine._CONDITIONAL_SKILL_CATEGORY in [c.name for c in kept]


def test_apply_ids_ignores_out_of_range_and_duplicates_and_respects_order():
    parsed = snippet_bank.parse(BASE_CV)
    chosen = engine._apply_ids(parsed.projects, [2, 99, 2, "x", 0], cap=5)
    assert chosen == [parsed.projects[2], parsed.projects[0]]


def test_apply_ids_non_list_returns_empty():
    parsed = snippet_bank.parse(BASE_CV)
    assert engine._apply_ids(parsed.projects, None, cap=3) == []


def test_apply_names_ignores_unknown_names():
    parsed = snippet_bank.parse(BASE_CV)
    chosen = engine._apply_names(parsed.skills, ["Technical Skills", "Not A Real Category"])
    assert [c.name for c in chosen] == ["Technical Skills"]


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


def test_apply_selection_trims_bullets_per_chosen_block():
    parsed = snippet_bank.parse(BASE_CV)
    dilitrust_idx = next(i for i, b in enumerate(parsed.experiences) if "DiliTrust" in b.text)
    chosen = engine._apply_selection(parsed.experiences, [dilitrust_idx], [[1, 3]], cap=2)
    assert len(chosen) == 1
    assert len(chosen[0].bullets()) == 2


def test_tailor_uses_llm_selection_when_available(monkeypatch):
    """The primary path: an LLM call (mirroring cv_tailoring_workflow.md) chooses
    which real blocks to keep, in the order it gives -- not re-sorted by date."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    parsed = snippet_bank.parse(BASE_CV)
    captured = {}

    def fake_select(job, experiences, projects, skill_names, feedback=None):
        captured["job"] = job
        captured["n_experiences"] = len(experiences)
        captured["n_projects"] = len(projects)
        captured["skill_names"] = skill_names
        captured["feedback"] = feedback
        return {
            "experience_ids": [1, 0],           # deliberately not date order
            "experience_bullets": [[], []],     # empty -> keep all bullets
            "project_ids": [3, 1],
            "project_bullets": [[], []],
            "skill_categories": ["Technical Skills", "Languages"],
            "reasoning": "test",
        }

    monkeypatch.setattr(engine.llm_select, "select", fake_select)
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job, parsed=parsed)

    assert captured["n_experiences"] == len(parsed.experiences)
    assert captured["n_projects"] == len(parsed.projects)
    assert captured["feedback"] is None
    # experience_ids [1, 0] means block 1 appears before block 0 in the output
    assert tex.index(parsed.experiences[1].text.strip()[:40]) < tex.index(parsed.experiences[0].text.strip()[:40])
    assert _n_projects(tex) == 2
    assert "Languages" in tex.split(r"\section{SKILLS}")[1]


def test_tailor_passes_feedback_through_to_the_llm_selection_call(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    captured = {}
    monkeypatch.setattr(engine.llm_select, "select", lambda job, e, p, s, feedback=None:
                        captured.update(feedback=feedback) or {
                            "experience_ids": [0], "experience_bullets": [[]],
                            "project_ids": [0], "project_bullets": [[]],
                            "skill_categories": ["Technical Skills"], "reasoning": "",
                        })
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    engine.tailor_tex(job, feedback="compiled to 3 pages, trim more")
    assert captured["feedback"] == "compiled to 3 pages, trim more"


def test_tailor_llm_selection_trims_bullets_within_kept_entries(monkeypatch):
    """This is the fix for the demonstrated gap: block-level selection alone
    kept a whole 5-bullet entry and left page 2 sparse. The LLM call can now
    also choose a subset of a kept entry's own bullets."""
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    parsed = snippet_bank.parse(BASE_CV)
    deepwise_idx = next(i for i, b in enumerate(parsed.experiences) if "DeepWise" in b.text)
    assert len(parsed.experiences[deepwise_idx].bullets()) == 5  # sanity: master has 5

    monkeypatch.setattr(engine.llm_select, "select", lambda job, e, p, s, feedback=None: {
        "experience_ids": [deepwise_idx],
        "experience_bullets": [[0, 2, 3]],   # keep only 3 of DeepWise's 5 bullets
        "project_ids": [0],
        "project_bullets": [[]],
        "skill_categories": ["Technical Skills"],
        "reasoning": "test",
    })
    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex = engine.tailor_tex(job, parsed=parsed)

    exp_section = tex.split(r"\section{PROFESSIONAL EXPERIENCE}")[1].split(r"\section{PROJECTS")[0]
    assert exp_section.count(r"\resumeItem{") == 3
    original = parsed.experiences[deepwise_idx].bullets()
    assert original[0] in exp_section and original[2] in exp_section and original[3] in exp_section
    assert original[1] not in exp_section and original[4] not in exp_section


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
        "experience_ids": [], "experience_bullets": [],
        "project_ids": [0], "project_bullets": [[]],
        "skill_categories": [], "reasoning": "",
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


def test_last_page_fill_ratio_none_without_pdftotext(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_pdftotext_path", lambda: None)
    assert engine._last_page_fill_ratio(tmp_path / "cv.pdf", 2) is None


def test_last_page_fill_ratio_none_for_single_page(tmp_path):
    assert engine._last_page_fill_ratio(tmp_path / "cv.pdf", 1) is None


def test_last_page_fill_ratio_computes_relative_density(tmp_path, monkeypatch):
    pages = {1: "line\n" * 20, 2: "line\n" * 5}
    monkeypatch.setattr(engine, "_page_text", lambda pdf, page: pages[page])
    ratio = engine._last_page_fill_ratio(tmp_path / "cv.pdf", 2)
    assert ratio == 0.25


def test_retry_feedback_none_when_fill_ratio_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_last_page_fill_ratio", lambda pdf, n: 0.8)
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    assert engine._retry_feedback(pdf, tmp_path) is None


def test_retry_feedback_requests_more_content_when_sparse(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_last_page_fill_ratio", lambda pdf, n: 0.1)
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    feedback = engine._retry_feedback(pdf, tmp_path)
    assert feedback is not None and "sparse" in feedback


def test_retry_feedback_requests_trim_when_compile_rejected_for_too_many_pages(tmp_path):
    (tmp_path / "cv.compile.log").write_text(
        "Compiled to 3 page(s), expected exactly 2. PDF kept for review.\n")
    feedback = engine._retry_feedback(None, tmp_path)
    assert feedback is not None and "Trim" in feedback


def test_retry_feedback_none_on_a_real_compile_error_not_a_length_issue(tmp_path):
    (tmp_path / "cv.compile.log").write_text("! Undefined control sequence.\nl.42 \\foo\n")
    assert engine._retry_feedback(None, tmp_path) is None


def test_tailor_job_retries_once_when_first_attempt_is_sparse(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine.provider, "available", lambda: False)  # deterministic fallback is fine here
    calls = []

    def fake_compile(tex, out_dir, name="cv", expected_pages=None):
        calls.append(tex)
        return out_dir / "cv.pdf"   # "succeeds" both times -- retry is driven by fill ratio, not page count

    monkeypatch.setattr(engine, "compile_tex", fake_compile)
    # Sparse on the first check, healthy on the second, so exactly one retry happens.
    ratios = iter([0.1, 0.9])
    monkeypatch.setattr(engine, "_last_page_fill_ratio", lambda pdf, n: next(ratios))

    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    tex_path, pdf_path = engine.tailor_job(job, 1, auto=True)

    assert len(calls) == 2   # first attempt + exactly one retry, not an unbounded loop
    assert pdf_path == tmp_path / "1-acme" / "cv.pdf"


def test_tailor_job_does_not_retry_when_first_attempt_is_already_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    calls = []
    monkeypatch.setattr(engine, "compile_tex", lambda tex, out_dir, name="cv", expected_pages=None:
                        calls.append(tex) or out_dir / "cv.pdf")
    monkeypatch.setattr(engine, "_last_page_fill_ratio", lambda pdf, n: 0.85)

    job = Job(source="x", external_id="1", title="ML Engineer", company="Acme", description="machine learning")
    engine.tailor_job(job, 1, auto=True)

    assert len(calls) == 1
