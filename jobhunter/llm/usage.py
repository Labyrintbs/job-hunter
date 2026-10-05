"""What the LLM calls cost, for the /usage page: logged calls (provider._log_call) summed by
step, model and day, priced from config/llm_prices.yaml, plus the subscription's own
session / weekly usage as `claude -p /usage` reports it."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import yaml

from ..config import REPO_ROOT
from . import provider

PRICES_PATH = REPO_ROOT / "config" / "llm_prices.yaml"
BASELINE_STEP = "baseline"
_PER = 1_000_000

_SESSION = re.compile(r"Current session:\s*(\d+)% used\s*·\s*resets ([^\n]+)")
_WEEK = re.compile(r"Current week \(all models\):\s*(\d+)% used\s*·\s*resets ([^\n]+)")


def load_prices(path=PRICES_PATH) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("models", {})


def input_total(row) -> int:
    """Every input token the call was charged for, whether new, cached or written to the cache."""
    return row["input_tokens"] + row["cache_read"] + row["cache_write_5m"] + row["cache_write_1h"]


def cost_of(row, prices: dict) -> float | None:
    """Notional list-price cost of one logged call, or None for a model with no price."""
    p = prices.get(row["model"])
    if not p:
        return None
    return (row["input_tokens"] * p["input"] + row["output_tokens"] * p["output"]
            + row["cache_read"] * p["cache_read"] + row["cache_write_5m"] * p["cache_write_5m"]
            + row["cache_write_1h"] * p["cache_write_1h"]) / _PER


def parse_quota(text: str) -> dict:
    """Session and weekly percentages from `claude -p /usage`; empty when the text has neither."""
    out: dict = {}
    if m := _SESSION.search(text or ""):
        out.update(session_pct=int(m.group(1)), session_resets=m.group(2).strip())
    if m := _WEEK.search(text or ""):
        out.update(week_pct=int(m.group(1)), week_resets=m.group(2).strip())
    return out


def fetch_quota(timeout: int = 40) -> dict:
    """The subscription's current usage, {} when the CLI is missing, on an API key, or fails."""
    path = provider._cli_path()
    if not path or provider._has_api_key():
        return {}
    try:
        proc = subprocess.run([path, "-p", "/usage"], capture_output=True, text=True,
                              timeout=timeout, env=provider._cli_env())
    except (subprocess.TimeoutExpired, OSError):
        return {}
    return parse_quota(proc.stdout)


def measure_baseline(model: str | None = None) -> dict:
    """One tiny call, logged as the 'baseline' step: its input tokens are what every call
    pays before our own prompt is counted."""
    provider.generate("Reply with: ok", max_tokens=20, model=model or provider.FAST_MODEL,
                      step=BASELINE_STEP)
    return {"model": model or provider.FAST_MODEL, "slim": provider.SLIM}


def _since(days: int | None, now: datetime) -> str:
    return "" if days is None else (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def summarize(rows, prices: dict, now: datetime | None = None) -> dict:
    """Totals per period, step, model and day, and the share of each step's input that is the
    CLI's own setup (the latest baseline call in the same mode) rather than our prompt."""
    now = now or datetime.now(timezone.utc)
    rows = [dict(r) for r in rows]
    baseline = {}
    for r in rows:
        if r["step"] == BASELINE_STEP and r["ok"]:
            baseline[r["slim"]] = r      # later rows replace earlier ones
    real = [r for r in rows if r["step"] != BASELINE_STEP]
    for r in real:
        r["input_total"] = input_total(r)
        r["cost"] = cost_of(r, prices)
        b = baseline.get(r["slim"])
        r["setup"] = min(input_total(b), r["input_total"]) if b and r["ok"] else 0

    def total(group) -> dict:
        costs = [r["cost"] for r in group if r["cost"] is not None]
        return {"calls": len(group), "failed": sum(1 for r in group if not r["ok"]),
                "input": sum(r["input_total"] for r in group),
                "output": sum(r["output_tokens"] for r in group),
                "setup": sum(r["setup"] for r in group), "cost": sum(costs),
                "unpriced": sum(1 for r in group if r["cost"] is None)}

    periods = []
    for label, days in (("Last 24 hours", 1), ("Last 7 days", 7), ("All time", None)):
        cutoff = _since(days, now)
        periods.append({"label": label, **total([r for r in real if r["at"] >= cutoff])})

    def grouped(key) -> list[dict]:
        groups = defaultdict(list)
        for r in real:
            groups[key(r)].append(r)
        out = [{"name": name, **total(g)} for name, g in groups.items()]
        out.sort(key=lambda g: (-g["cost"], -g["input"]))
        return out

    steps, models = grouped(lambda r: r["step"]), grouped(lambda r: r["model"])
    grand = sum(s["cost"] for s in steps) or 1.0
    for s in steps:
        s["cost_share"] = s["cost"] / grand
        s["avg_input"] = s["input"] // s["calls"] if s["calls"] else 0
        s["avg_setup"] = s["setup"] // s["calls"] if s["calls"] else 0
    days = sorted(grouped(lambda r: r["at"][:10]), key=lambda g: g["name"])[-14:]
    latest = baseline.get(provider.SLIM) or (next(iter(baseline.values())) if baseline else None)
    return {"periods": periods, "steps": steps, "models": models, "days": days,
            "baseline": ({"tokens": input_total(latest), "model": latest["model"],
                          "slim": bool(latest["slim"]), "at": latest["at"],
                          "cost": cost_of(latest, prices)} if latest else None),
            "slim_now": provider.SLIM}
