from jobhunter.apply import cover_letter as CL
from jobhunter.models import Job


def _job():
    return Job(source="wttj", external_id="1", title="ML Engineer", company="Acme",
              description="we build production ML systems")


def test_draft_injects_judge_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job(), judge_context="Rated 'good' fit (70/100): solid domain overlap")
    assert "FIT-JUDGE'S OWN ASSESSMENT" in captured["prompt"]
    assert "Rated 'good' fit (70/100): solid domain overlap" in captured["prompt"]


def test_draft_grounds_the_letter_in_the_tailored_cv_when_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job(), cv_text="TAILORED_CV_ONLY_CONTENT")
    assert "TAILORED_CV_ONLY_CONTENT" in captured["prompt"]
    assert "exact CV sent with this application" in captured["prompt"]
    assert "NeRF" not in captured["prompt"]        # the full base CV is not also included


def test_draft_without_a_tailored_cv_uses_the_whole_base_cv_uncapped(monkeypatch):
    from jobhunter.llm.profile import profile_text
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job())
    assert profile_text() in captured["prompt"]    # incl. SKILLS at the end, no 6000-char cut


def test_draft_without_judge_context_omits_the_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job())
    assert "FIT-JUDGE'S OWN ASSESSMENT" not in captured["prompt"]
