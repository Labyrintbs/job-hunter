from jobhunter.models import Job
from jobhunter.tailor import select as S


def _job():
    return Job(source="wttj", external_id="1", title="ML Engineer", company="Acme",
              description="we build production ML systems")


def test_select_injects_judge_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(S.provider, "generate_json",
                        lambda prompt, **kw: captured.update(prompt=prompt) or {
                            "experience_ids": [], "experience_bullets": [],
                            "project_ids": [], "project_bullets": [],
                            "skill_categories": [], "reasoning": "",
                        })
    S.select(_job(), [], [], [], judge_context="Rated 'strong' fit (89/100): great domain match")
    assert "FIT-JUDGE'S OWN ASSESSMENT" in captured["prompt"]
    assert "Rated 'strong' fit (89/100): great domain match" in captured["prompt"]


def test_select_without_judge_context_omits_the_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(S.provider, "generate_json",
                        lambda prompt, **kw: captured.update(prompt=prompt) or {
                            "experience_ids": [], "experience_bullets": [],
                            "project_ids": [], "project_bullets": [],
                            "skill_categories": [], "reasoning": "",
                        })
    S.select(_job(), [], [], [])
    assert "FIT-JUDGE'S OWN ASSESSMENT" not in captured["prompt"]
