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


def test_judge_uses_condensed_profile_not_full(monkeypatch):
    # The judge should get background/skills, not verbose project bullets.
    captured = {}
    monkeypatch.setattr(J.provider, "generate_json",
                        lambda prompt, system=None, **kw: captured.update(prompt=prompt)
                        or {"score": 50, "verdict": "stretch", "reasons": ""})
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="C", description="d")
    J.judge(job)
    assert "SKILLS" in captured["prompt"]
    assert "Point Cloud" not in captured["prompt"]


def test_judge_passes_the_result_schema_to_the_provider(monkeypatch):
    # This is what actually prevents truncated/invalid JSON on the CLI backend -- not
    # max_tokens, which the CLI has no equivalent for. See provider._generate_cli.
    captured = {}
    monkeypatch.setattr(J.provider, "generate_json",
                        lambda prompt, system=None, **kw: captured.update(kw)
                        or {"score": 50, "verdict": "stretch", "seniority": "junior",
                            "min_years": 0, "reasons": ""})
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="C", description="d")
    J.judge(job)
    assert captured["json_schema"] == J.RESULT_SCHEMA


def test_judge_passes_up_to_8000_chars_of_description(monkeypatch):
    captured = {}
    monkeypatch.setattr(J.provider, "generate_json",
                        lambda prompt, system=None, **kw: captured.update(prompt=prompt)
                        or {"score": 50, "verdict": "stretch", "reasons": ""})
    # markers straddle the old 4000-char cutoff and the new 8000-char one
    long_desc = ("x" * 5000) + "WITHIN_CUTOFF" + ("x" * 2987) + "PAST_CUTOFF" + ("x" * 2000)
    job = Job(source="wttj", external_id="1", title="ML Engineer", company="C", description=long_desc)
    J.judge(job)
    assert "WITHIN_CUTOFF" in captured["prompt"]   # would be cut off at the old 4000-char limit
    assert "PAST_CUTOFF" not in captured["prompt"]   # still bounded at 8000, not unlimited
