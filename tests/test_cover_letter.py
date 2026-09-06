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


def test_draft_without_judge_context_omits_the_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(CL.provider, "generate",
                        lambda prompt, **kw: captured.update(prompt=prompt) or "cover letter body")
    CL.draft(_job())
    assert "FIT-JUDGE'S OWN ASSESSMENT" not in captured["prompt"]
