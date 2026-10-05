"""Provider-agnostic LLM seam.

Order of preference:
  1. Anthropic API via the `anthropic` SDK when ANTHROPIC_API_KEY is set.
  2. The local `claude` CLI (`claude -p`) — uses the user's Claude subscription.
  3. Unavailable -> callers fall back to their deterministic path.

Callers should degrade gracefully: check `available()` or catch LLMUnavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_TIMEOUT = 180
LOG_CALLS = True     # every call is recorded for the /usage page; tests switch this off
_DEFAULT_SYSTEM = "You are a concise, factual assistant."
# JOBHUNTER_CLI_SLIM=1 starts the CLI without its default setup (Claude Code's own system
# prompt, tools, skills, MCP servers, settings), which otherwise adds tens of thousands of
# input tokens to every call; the call then carries only our own system prompt.
SLIM = os.environ.get("JOBHUNTER_CLI_SLIM") == "1"
_SLIM_FLAGS = ["--tools", "", "--disable-slash-commands", "--strict-mcp-config",
               "--setting-sources", "", "--no-session-persistence"]
MODEL = os.environ.get("JOBHUNTER_MODEL", "claude-sonnet-5-5")
# Cheaper model for calls that only classify or pick ids (block selection, fit judge);
# pass model=FAST_MODEL. Anything that writes text for the CV or letter uses MODEL.
FAST_MODEL = os.environ.get("JOBHUNTER_FAST_MODEL", "claude-sonnet-5")

# cron runs with a bare minimal PATH that won't include where `claude` actually lives
# (e.g. ~/.local/bin), so PATH-only lookup silently disables the judge under cron.
_CLI_FALLBACKS = [
    Path.home() / ".local" / "bin" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]

# macOS cron/launchd's default PATH never includes these, so `claude`'s own
# SessionEnd hook (which shells out to `node`) fails with "node: command not
# found" even after `claude` itself was found via _CLI_FALLBACKS above.
_CLI_EXTRA_PATHS = ["/usr/local/bin", "/opt/homebrew/bin"]


def _nvm_bin_dirs() -> list[str]:
    """Node managed via nvm (rather than Homebrew) doesn't live at a fixed path --
    it's versioned under ~/.nvm/versions/node/<version>/bin. Glob for whatever's
    installed rather than hardcoding a version that will drift on the next nvm
    upgrade."""
    nvm_versions = Path.home() / ".nvm" / "versions" / "node"
    if not nvm_versions.exists():
        return []
    return [str(p / "bin") for p in nvm_versions.iterdir() if (p / "bin").is_dir()]


def _cli_env() -> dict:
    env = os.environ.copy()
    extra = [p for p in _CLI_EXTRA_PATHS + _nvm_bin_dirs() if p not in env.get("PATH", "")]
    if extra:
        env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


class LLMUnavailable(RuntimeError):
    pass


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _cli_path() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in _CLI_FALLBACKS:
        if p.exists():
            return str(p)
    return None


def _has_cli() -> bool:
    return _cli_path() is not None


def available() -> bool:
    return _has_api_key() or _has_cli()


def backend() -> str:
    if _has_api_key():
        return "anthropic-api"
    if _has_cli():
        return "claude-cli"
    return "none"


def _log_call(step: str, model: str, backend_name: str, prompt_chars: int, usage: dict | None = None,
              duration_ms: int = 0, cost_reported: float = 0.0, slim: bool = False,
              error: str = "") -> None:
    """Record one call for the /usage page. Logging must never break a call, so any failure
    (database locked, no database in a test) is swallowed."""
    if not LOG_CALLS:
        return
    usage = usage or {}
    cache = usage.get("cache_creation") or {}
    write_1h = cache.get("ephemeral_1h_input_tokens", 0) or 0
    write_5m = cache.get("ephemeral_5m_input_tokens", 0) or 0
    if not (write_1h or write_5m):
        write_5m = usage.get("cache_creation_input_tokens", 0) or 0
    try:
        from .. import db
        with db.connect() as conn:
            db.record_llm_call(
                conn, step=step or "other", model=model, backend=backend_name,
                input_tokens=usage.get("input_tokens", 0) or 0,
                cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                cache_write_5m=write_5m, cache_write_1h=write_1h,
                output_tokens=usage.get("output_tokens", 0) or 0,
                cost_reported=cost_reported, duration_ms=duration_ms,
                prompt_chars=prompt_chars, slim=int(slim), ok=int(not error), error=error[:300])
    except Exception:
        pass


def _generate_api(prompt: str, system: str | None, max_tokens: int, model: str | None = None,
                  step: str = "") -> str:
    import anthropic

    client = anthropic.Anthropic()
    model = model or MODEL
    system = system or _DEFAULT_SYSTEM
    started = time.monotonic()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _log_call(step, model, "anthropic-api", len(prompt) + len(system), error=str(exc))
        raise
    usage = {k: getattr(msg.usage, k, 0) for k in
             ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    _log_call(step, model, "anthropic-api", len(prompt) + len(system), usage,
              duration_ms=int((time.monotonic() - started) * 1000))
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip()


def _parse_cli_output(stdout: str, json_schema: dict | None) -> tuple[str, dict]:
    """(answer text, the CLI's result object) from `--output-format json`; plain text
    stdout (no JSON envelope) comes back as itself with an empty object."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout.strip(), {}
    if not isinstance(data, dict) or "result" not in data:
        return stdout.strip(), {}
    if json_schema and data.get("structured_output") is not None:
        return json.dumps(data["structured_output"]), data
    return (data.get("result") or "").strip(), data


def _generate_cli(prompt: str, system: str | None, timeout: int,
                  json_schema: dict | None = None, model: str | None = None,
                  step: str = "") -> str:
    model = model or MODEL
    cmd = [_cli_path() or "claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if SLIM:
        cmd += ["--system-prompt", system or _DEFAULT_SYSTEM, *_SLIM_FLAGS]
    elif system:
        cmd += ["--append-system-prompt", system]
    if json_schema:
        # Structured-output validation -- the CLI has no --max-tokens equivalent, so this
        # (not a token cap) is what actually prevents truncated/invalid JSON on longer
        # responses: the model is constrained to emit a complete object matching the schema.
        cmd += ["--json-schema", json.dumps(json_schema)]
    chars = len(prompt) + len(system or "")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_cli_env())
    except subprocess.TimeoutExpired:
        _log_call(step, model, "claude-cli", chars, slim=SLIM, error=f"timeout after {timeout}s")
        raise
    text, data = _parse_cli_output(proc.stdout, json_schema)
    meta = dict(usage=data.get("usage"), duration_ms=data.get("duration_ms") or 0,
                cost_reported=data.get("total_cost_usd") or 0.0, slim=SLIM)
    if proc.returncode != 0:
        # The CLI prints errors like usage limits / "Not logged in" to stdout, not stderr.
        message = f"claude CLI failed (rc={proc.returncode}): {(proc.stderr or text).strip()[:300]}"
        _log_call(step, model, "claude-cli", chars, error=message, **meta)
        raise LLMUnavailable(message)
    _log_call(step, model, "claude-cli", chars, **meta)
    return text


def generate(prompt: str, system: str | None = None, max_tokens: int = 1500,
             timeout: int = DEFAULT_TIMEOUT, json_schema: dict | None = None,
             model: str | None = None, step: str = "") -> str:
    """`step` names the pipeline step for the usage log (select, judge, dedup, ...)."""
    if _has_api_key():
        # max_tokens applies here; the CLI path below has no equivalent knob.
        return _generate_api(prompt, system, max_tokens, model, step=step)
    if _has_cli():
        return _generate_cli(prompt, system, timeout, json_schema=json_schema, model=model, step=step)
    raise LLMUnavailable("no ANTHROPIC_API_KEY and no `claude` CLI on PATH")


def generate_json(prompt: str, system: str | None = None, json_schema: dict | None = None,
                  **kw) -> dict:
    """Generate and parse a JSON object. Pass json_schema to constrain the CLI backend to
    valid, complete, schema-conformant output (recommended -- see _generate_cli); without
    it, or on the API backend, this tolerates prose or code fences around a JSON object."""
    raw = generate(prompt, system=system, json_schema=json_schema, **kw)
    if json_schema:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass   # e.g. the API backend doesn't enforce the schema -- fall through
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"no JSON object in LLM output: {raw[:200]}")
    return json.loads(m.group(0))
