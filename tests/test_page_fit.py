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


def test_the_densest_selection_keeping_every_bullet_fits(tmp_path, monkeypatch):
    # DiliTrust + DeepWise and the Job Hunter, Art History and Polyps projects with all
    # their bullets (an empty list keeps them all): denser than the LLM normally picks,
    # and it overflows to a third page without the spacing fix after \projectdesc
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
        "experience_ids": [0, 1], "experience_bullets": [[], []],
        "project_ids": [0, 1, 4], "project_bullets": [[], [], []],
        "skill_categories": [c.name for c in snippet_bank.parse(engine.BASE_CV).skills], "reasoning": ""})
    monkeypatch.setattr(summary, "generate_summary",
                        lambda *a, **k: LONGEST.format(title="Data Scientist"))
    job = Job(source="x", external_id="1", title="Data Scientist", company="Acme", description="data")
    tex = engine.tailor_tex(job, role_category="AI")
    assert "Job Hunter" in tex and "hands-on LLM engineering experience" in tex
    pdf = engine.compile_tex(tex, tmp_path, name="cv", expected_pages=2)
    assert pdf is not None, (tmp_path / "cv.compile.log").read_text()


@pytest.mark.parametrize("kind", sorted(JOBS))
def test_the_tailored_cv_is_exactly_two_pages(kind, tmp_path, monkeypatch):
    category, title, description = JOBS[kind]
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: LONGEST.format(title=title))
    job = Job(source="x", external_id="1", title=title, company="Acme", description=description)
    tex = engine.tailor_tex(job, role_category=category)
    assert "hands-on LLM engineering experience" in tex   # the long summary is really in the CV
    pdf = engine.compile_tex(tex, tmp_path, name="cv", expected_pages=2)
    assert pdf is not None, (tmp_path / "cv.compile.log").read_text()
