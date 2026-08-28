from jobhunter.llm import judge as J
from jobhunter.models import Job


def test_judge_injects_preferences(monkeypatch):
    captured = {}

    def fake_generate_json(prompt, system=None, **kw):
        captured["prompt"] = prompt
        return {"score": 72, "verdict": "good", "seniority": "junior",
                "min_years": 1, "reasons": "solid fit"}

    monkeypatch.setattr(J.provider, "generate_json", fake_generate_json)
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="C", description="d")
    out = J.judge(job, preferences="- Avoid blockchain\n- Prefer MLOps")
    assert "Avoid blockchain" in captured["prompt"]
    assert out["score"] == 72 and out["seniority"] == "junior" and out["min_years"] == 1


def test_judge_without_preferences_has_no_pref_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(J.provider, "generate_json",
                        lambda prompt, system=None, **kw: captured.update(prompt=prompt)
                        or {"score": 50, "verdict": "stretch", "reasons": ""})
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="C", description="d")
    J.judge(job)
    assert "LEARNED PREFERENCES" not in captured["prompt"]
