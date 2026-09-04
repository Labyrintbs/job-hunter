import re

from jobhunter.models import Job
from jobhunter.tailor import engine, snippet_bank
from jobhunter.tailor.engine import BASE_CV


def test_parse_blocks():
    parsed = snippet_bank.parse(BASE_CV)
    assert len(parsed.projects) == 6
    assert len(parsed.experiences) == 3
    assert parsed.heading_line


def test_terms_boundary_no_false_positive():
    # 'ct' must not match inside 'detection' / 'structural'
    assert "ct" not in snippet_bank.terms_in("object detection and structural work")
    assert "cnn" in snippet_bank.terms_in("we use CNNs heavily")
    assert "fine-tun" in snippet_bank.terms_in("experience with fine-tuning models")


def test_reorder_floats_relevant_experience_first():
    parsed = snippet_bank.parse(BASE_CV)
    nlp = Job(source="x", external_id="1", title="LLM Engineer", company="A",
              description="LLM fine-tuning, GRPO, prompt engineering, entity extraction, NLP")
    ranked = engine._rank(parsed.experiences, engine._job_terms(nlp))
    assert "DiliTrust" in ranked[0].text


def test_tailor_uses_fixed_generic_tagline_and_is_valid_latex_structure():
    # Tagline is a fixed generic line, no per-job "targeting <role> at <company>"
    # clause -- see templates/cv_tailoring_workflow.md's header-tagline rule.
    job = Job(source="x", external_id="1", title="ML Engineer & Data H/F",
              company="Acme & Co", description="machine learning")
    tex = engine.tailor_tex(job)
    assert engine._tagline() in tex
    assert "targeting" not in tex
    assert tex.count(r"\begin{document}") == 1
    assert tex.count(r"\end{document}") == 1


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
