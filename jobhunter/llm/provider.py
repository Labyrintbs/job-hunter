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
from pathlib import Path

DEFAULT_TIMEOUT = 180

# cron runs with a bare minimal PATH that won't include where `claude` actually lives
# (e.g. ~/.local/bin), so PATH-only lookup silently disables the judge under cron.
_CLI_FALLBACKS = [
    Path.home() / ".local" / "bin" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]


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


def _generate_api(prompt: str, system: str | None, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=os.environ.get("JOBHUNTER_MODEL", "claude-sonnet-5"),
        max_tokens=max_tokens,
        system=system or "You are a concise, factual assistant.",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip()


def _generate_cli(prompt: str, system: str | None, timeout: int) -> str:
    cmd = [_cli_path() or "claude", "-p", prompt]
    if system:
        cmd += ["--append-system-prompt", system]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise LLMUnavailable(f"claude CLI failed: {proc.stderr[:300]}")
    return proc.stdout.strip()


def generate(prompt: str, system: str | None = None, max_tokens: int = 1500,
             timeout: int = DEFAULT_TIMEOUT) -> str:
    if _has_api_key():
        return _generate_api(prompt, system, max_tokens)
    if _has_cli():
        return _generate_cli(prompt, system, timeout)
    raise LLMUnavailable("no ANTHROPIC_API_KEY and no `claude` CLI on PATH")


def generate_json(prompt: str, system: str | None = None, **kw) -> dict:
    """Generate and parse a JSON object, tolerating prose or code fences around it."""
    raw = generate(prompt, system=system, **kw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"no JSON object in LLM output: {raw[:200]}")
    return json.loads(m.group(0))
