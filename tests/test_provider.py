import json

from jobhunter.llm import provider


def test_backend_prefers_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert provider.backend() == "anthropic-api"
    assert provider.available() is True


def test_backend_falls_back_to_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(provider.shutil, "which", lambda name: "/usr/bin/claude")
    assert provider.backend() == "claude-cli"


def test_unavailable_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(provider.shutil, "which", lambda name: None)
    monkeypatch.setattr(provider, "_CLI_FALLBACKS", [])   # don't leak the real machine's claude
    assert provider.available() is False
    assert provider.backend() == "none"
    try:
        provider.generate("hi")
        assert False, "should have raised"
    except provider.LLMUnavailable:
        pass


def test_cli_falls_back_to_known_install_paths(monkeypatch, tmp_path):
    # Simulates cron's bare PATH: `which` finds nothing, but the binary exists at a
    # known fallback location (e.g. ~/.local/bin, where the claude CLI installs).
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\necho hi")
    monkeypatch.setattr(provider.shutil, "which", lambda name: None)
    monkeypatch.setattr(provider, "_CLI_FALLBACKS", [fake_claude])
    assert provider._has_cli() is True
    assert provider._cli_path() == str(fake_claude)
    assert provider.backend() == "claude-cli"


def test_generate_json_extracts_object(monkeypatch):
    monkeypatch.setattr(provider, "generate",
                        lambda *a, **k: 'here you go:\n```json\n{"score": 80, "verdict": "good"}\n```')
    data = provider.generate_json("x")
    assert data["score"] == 80 and data["verdict"] == "good"


def test_generate_json_parses_strict_schema_output_directly(monkeypatch):
    # With json_schema, the CLI is expected to emit a bare, complete object -- no
    # prose/fences to strip, so this should parse without the regex-extraction fallback.
    monkeypatch.setattr(provider, "generate", lambda *a, **k: '{"score": 42}')
    data = provider.generate_json("x", json_schema={"type": "object"})
    assert data["score"] == 42


def test_generate_json_falls_back_to_extraction_if_schema_output_isnt_clean(monkeypatch):
    # e.g. the API backend, which doesn't enforce json_schema at all.
    monkeypatch.setattr(provider, "generate",
                        lambda *a, **k: 'sure:\n```json\n{"score": 7}\n```')
    data = provider.generate_json("x", json_schema={"type": "object"})
    assert data["score"] == 7


def test_generate_cli_passes_json_schema_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = '{"score": 1}'
            stderr = ""
        return R()

    monkeypatch.setattr(provider.subprocess, "run", fake_run)
    monkeypatch.setattr(provider, "_cli_path", lambda: "/usr/bin/claude")
    schema = {"type": "object", "required": ["score"]}
    provider._generate_cli("prompt", None, 30, json_schema=schema)
    assert "--json-schema" in captured["cmd"]
    idx = captured["cmd"].index("--json-schema")
    assert captured["cmd"][idx + 1] == json.dumps(schema)
