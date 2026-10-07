import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jobhunter import db
from jobhunter.llm import provider, usage
from jobhunter.web.app import app

PRICES = {"m": {"input": 2.0, "output": 10.0, "cache_read": 0.2,
                "cache_write_5m": 2.5, "cache_write_1h": 5.0}}


def _row(step="judge", model="m", at="2026-10-05 10:00:00", ok=1, slim=0, **tokens):
    row = dict(step=step, model=model, at=at, ok=ok, slim=slim, input_tokens=0, cache_read=0,
               cache_write_5m=0, cache_write_1h=0, output_tokens=0)
    row.update(tokens)
    return row


def _envelope(text="hi", structured=None, is_error=False):
    data = {"result": text, "is_error": is_error, "duration_ms": 1234, "total_cost_usd": 0.05,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 50,
                      "cache_creation": {"ephemeral_1h_input_tokens": 50,
                                         "ephemeral_5m_input_tokens": 0}}}
    if structured is not None:
        data["structured_output"] = structured
    return json.dumps(data)


def test_cost_is_the_token_counts_times_the_model_prices():
    row = _row(input_tokens=1_000_000, output_tokens=100_000, cache_read=1_000_000, cache_write_1h=1_000_000)
    assert usage.cost_of(row, PRICES) == pytest.approx(2.0 + 1.0 + 0.2 + 5.0)
    assert usage.cost_of(_row(model="unknown"), PRICES) is None


def test_input_total_counts_new_cached_and_written_tokens():
    assert usage.input_total(_row(input_tokens=1, cache_read=2, cache_write_5m=3, cache_write_1h=4)) == 10


def test_quota_percentages_are_read_from_the_usage_text():
    text = ("Current session: 8% used · resets Oct 5, 4pm (Europe/Paris)\n"
            "Current week (all models): 34% used · resets Oct 10, 9am (Europe/Paris)\n")
    assert usage.parse_quota(text) == {
        "session_pct": 8, "session_resets": "Oct 5, 4pm (Europe/Paris)",
        "week_pct": 34, "week_resets": "Oct 10, 9am (Europe/Paris)"}
    assert usage.parse_quota("something else") == {}


def test_summary_splits_each_steps_input_into_cli_setup_and_our_prompt():
    rows = [
        _row("baseline", input_tokens=10, cache_write_1h=990),                 # setup = 1,000
        _row("judge", input_tokens=10, cache_read=1_990, output_tokens=100),   # 2,000 in
        _row("judge", input_tokens=10, cache_read=2_990, output_tokens=300),   # 3,000 in
        _row("dedup", input_tokens=500, output_tokens=50, ok=0),               # failed, below setup
    ]
    s = usage.summarize(rows, PRICES, now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    steps = {x["name"]: x for x in s["steps"]}
    assert "baseline" not in steps
    assert steps["judge"]["calls"] == 2 and steps["judge"]["avg_input"] == 2_500
    assert steps["judge"]["avg_setup"] == 1_000 and steps["judge"]["output"] == 400
    assert steps["dedup"]["failed"] == 1 and steps["dedup"]["setup"] == 0
    assert s["baseline"]["tokens"] == 1_000
    assert s["periods"][2]["calls"] == 3                       # the baseline call is not counted


def test_periods_count_only_calls_inside_their_window():
    rows = [_row(at="2026-10-05 11:00:00", input_tokens=1), _row(at="2026-10-01 11:00:00", input_tokens=1),
            _row(at="2026-09-01 11:00:00", input_tokens=1)]
    s = usage.summarize(rows, PRICES, now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
    assert [p["calls"] for p in s["periods"]] == [1, 2, 3]


def test_cli_answer_comes_from_the_structured_output_when_a_schema_is_given():
    text, data = provider._parse_cli_output(_envelope('{"a":1}', structured={"a": 1}), {"type": "object"})
    assert json.loads(text) == {"a": 1} and data["duration_ms"] == 1234
    assert provider._parse_cli_output(_envelope("plain"), None)[0] == "plain"
    assert provider._parse_cli_output("not json at all", None) == ("not json at all", {})


def _run_returning(monkeypatch, stdout, returncode=0):
    class R:
        pass
    r = R()
    r.stdout, r.stderr, r.returncode = stdout, "", returncode
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return r

    monkeypatch.setattr(provider.subprocess, "run", fake_run)
    monkeypatch.setattr(provider, "_cli_path", lambda: "/usr/bin/claude")
    return seen


def test_a_cli_call_is_logged_with_its_step_and_token_counts(tmp_db, monkeypatch):
    monkeypatch.setattr(provider, "LOG_CALLS", True)
    seen = _run_returning(monkeypatch, _envelope("answer"))
    assert provider._generate_cli("prompt", "system", 30, model="m", step="judge") == "answer"
    assert seen["cmd"][seen["cmd"].index("--output-format") + 1] == "json"
    with db.connect() as conn:
        (row,) = db.llm_calls_since(conn)
    assert (row["step"], row["model"], row["backend"], row["ok"]) == ("judge", "m", "claude-cli", 1)
    assert (row["input_tokens"], row["cache_read"], row["cache_write_1h"], row["output_tokens"]) == (10, 100, 50, 5)
    assert row["cost_reported"] == 0.05 and row["prompt_chars"] == len("prompt") + len("system")


def test_a_failed_cli_call_is_logged_with_the_error(tmp_db, monkeypatch):
    monkeypatch.setattr(provider, "LOG_CALLS", True)
    _run_returning(monkeypatch, _envelope("You've hit your session limit", is_error=True), returncode=1)
    with pytest.raises(provider.LLMUnavailable, match="session limit"):
        provider._generate_cli("p", None, 30, step="dedup")
    with db.connect() as conn:
        (row,) = db.llm_calls_since(conn)
    assert row["ok"] == 0 and "session limit" in row["error"] and row["step"] == "dedup"


def test_slim_mode_replaces_the_default_setup_instead_of_appending_to_it(monkeypatch):
    seen = _run_returning(monkeypatch, _envelope())
    monkeypatch.setattr(provider, "SLIM", True)
    provider._generate_cli("p", "my system", 30)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--system-prompt") + 1] == "my system" and "--append-system-prompt" not in cmd
    assert "--disable-slash-commands" in cmd and "--strict-mcp-config" in cmd


def test_a_single_call_can_force_slim_on_or_off_whatever_the_global_setting(monkeypatch):
    seen = _run_returning(monkeypatch, _envelope())
    monkeypatch.setattr(provider, "SLIM", False)
    provider._generate_cli("p", "sys", 30, slim=True)
    assert "--system-prompt" in seen["cmd"] and "--append-system-prompt" not in seen["cmd"]
    monkeypatch.setattr(provider, "SLIM", True)
    provider._generate_cli("p", "sys", 30, slim=False)
    assert "--append-system-prompt" in seen["cmd"] and "--system-prompt" not in seen["cmd"]
    provider._generate_cli("p", "sys", 30)      # no override: the global setting
    assert "--system-prompt" in seen["cmd"]


def test_the_dedup_call_is_slim_only_when_the_config_switch_is_on(monkeypatch):
    from jobhunter.llm import dedup
    from jobhunter.models import Job
    got = []
    monkeypatch.setattr(provider, "generate_json", lambda *a, **kw: got.append(kw["slim"]) or
                        {"verdict": "same", "confidence": "high", "reason": "r"})
    job = Job(source="wttj", external_id="1", title="t", company="c", location="l", description="d")
    monkeypatch.setattr("jobhunter.config.load_search_config", lambda: {"llm": {"dedup_slim": True}})
    dedup.compare(job, job)
    monkeypatch.setattr("jobhunter.config.load_search_config", lambda: {})
    dedup.compare(job, job)                       # switch off: follow the global setting
    dedup.compare(job, job, slim=False)           # an explicit choice wins over the config
    assert got == [True, None, False]


def test_the_usage_page_renders_logged_calls(tmp_db):
    with db.connect() as conn:
        db.record_llm_call(conn, step="cv selection", model="claude-sonnet-5", backend="claude-cli",
                           input_tokens=10, cache_read=30_000, output_tokens=500, slim=0)
        db.record_llm_call(conn, step="baseline", model="claude-sonnet-5", backend="claude-cli",
                           input_tokens=10, cache_read=20_000, slim=0)
    page = TestClient(app).get("/usage")
    assert page.status_code == 200
    assert "cv selection" in page.text and "20,010" in page.text       # the latest baseline's tokens
