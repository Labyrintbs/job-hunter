"""Layout regression: real CVs built from cv_base.tex must stay at exactly two pages.

Uses the keyword-fallback selection (no LLM) and a summary at the validator's
maximum length, so a change to the base CV's content or layout that pushes the
longest realistic output onto a third page fails here, not in a cron run.
"""
import shutil

import pytest

from jobhunter.models import Job
from jobhunter.tailor import engine, snippet_bank, summary

pytestmark = pytest.mark.skipif(
    shutil.which("latexmk", path=engine._tex_env()["PATH"]) is None,
    reason="latexmk is not installed")

LONGEST = ("Machine Learning Engineer with hands-on LLM engineering experience, from LangGraph agentic "
           "pipelines and GRPO fine-tuning to LLM-as-judge evaluation, gained during an ML internship on "
           "multilingual legal extraction in Paris. Also built clinical CT and CTA segmentation models "
           "deployed in a production annotation pipeline. This background suits the {title} role at Acme.")

JOBS = {
    "llm": ("AI", "LLM Engineer", "LLM fine-tuning, agents, prompt engineering and evaluation"),
    "vision": ("CV", "Computer Vision Engineer", "image segmentation, point cloud, medical imaging"),
    "nlp": ("NLP", "NLP Engineer", "nlp, named entity extraction, transformers"),
}


def test_the_longest_summary_used_here_is_valid_and_at_the_limit():
    doc = snippet_bank.parse(engine.BASE_CV).document
    text = LONGEST.format(title="Data Scientist")
    assert summary.validate_summary(text, f"{doc} Data Scientist Acme", "Acme") == ""
    assert len(text.split()) >= summary.MAX_WORDS - 8


def _tailor(tmp_path, monkeypatch, job, language="en", role_category="AI"):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    return engine.tailor_job(job, 1, auto=True, role_category=role_category, language=language)


def _pages(pdf):
    layout = engine._page_layout(pdf)
    return layout.pages if layout else None


def test_the_densest_selection_is_fitted_to_exactly_two_pages(tmp_path, monkeypatch):
    # DiliTrust + DeepWise and three projects with every bullet and every skills item: far
    # more than two pages, so the fit step has to trim by measuring. Work experience stays whole.
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
        "experience_ids": [0, 1], "experience_scores": [[], []],
        "project_ids": [0, 1, 4], "project_scores": [[], [], []],
        "skill_scores": [], "reasoning": ""})
    monkeypatch.setattr(summary, "generate_summary",
                        lambda *a, **k: LONGEST.format(title="Data Scientist"))
    job = Job(source="x", external_id="1", title="Data Scientist", company="Acme", description="data")
    res = _tailor(tmp_path, monkeypatch, job)
    assert res.pdf_path is not None, (tmp_path / "1-acme" / "cv.compile.log").read_text()
    assert _pages(res.pdf_path) == 2
    tex = res.tex_path.read_text()
    assert "Job Hunter" in tex and "hands-on LLM engineering experience" in tex
    parsed = snippet_bank.parse(engine.BASE_CV)
    for block in parsed.experiences[:2]:
        assert all(b in tex for b in block.bullets())          # every work-experience bullet survives
    assert next(res.tex_path.parent.glob("cv-*.fit.txt")).read_text().startswith("status: fit")


@pytest.mark.parametrize("kind", sorted(JOBS))
def test_the_keyword_fallback_cv_is_exactly_two_pages(kind, tmp_path, monkeypatch):
    category, title, description = JOBS[kind]
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: LONGEST.format(title=title))
    job = Job(source="x", external_id="1", title=title, company="Acme", description=description)
    res = _tailor(tmp_path, monkeypatch, job, role_category=category)
    assert res.pdf_path is not None, (tmp_path / "1-acme" / "cv.compile.log").read_text()
    assert _pages(res.pdf_path) == 2


def test_a_french_cv_is_fitted_to_exactly_two_pages_without_a_length_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    job = Job(source="x", external_id="1", title="Ingénieur LLM", company="Acme",
              description="LLM, fine-tuning, agents, evaluation")
    res = _tailor(tmp_path, monkeypatch, job, language="fr")
    assert res.pdf_path is not None and res.lang == "fr", (tmp_path / "1-acme" / "cv.compile.log").read_text()
    assert _pages(res.pdf_path) == 2
