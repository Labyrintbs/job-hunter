"""Rule-based scoring for a Paris junior ML-engineer hunt.

Deliberately loose on seniority so junior-hostile phrasing ("2+ years") does not
zero out a job, but hard-excludes internships/alternance (want CDI/full-time).
This is the seam where an LLM judge slots in later (see match_reasons on the job).
"""
from __future__ import annotations

from .models import Job

# Seniority signals that count against a junior fit (soft penalty, never a hard drop).
SENIOR_TERMS = ["senior", "lead", "principal", "staff", "head of", "confirmé", "confirme", "expert", "director"]


def _text(job: Job) -> str:
    return f"{job.title} {job.contract_type} {job.description}".lower()


def is_excluded(job: Job, config: dict) -> str | None:
    text = f"{job.title} {job.contract_type}".lower()
    for term in config.get("exclude_terms", []):
        if term.lower() in text:
            return f"excluded: contains '{term}'"
    company = job.company.lower()
    for blocked in config.get("company_blocklist", []):
        if blocked.lower() == company:
            return f"excluded: blocked company '{job.company}'"
    return None


def _geo_tier(job: Job, config: dict) -> tuple[int, str]:
    """Returns (bonus_points, reason). Paris/IDF ranks above other-France (mobility)."""
    loc = job.location.lower()
    if any(l.lower() in loc for l in config.get("locations", [])):
        return 20, f"geo: Paris/IDF ({job.location})"
    if not loc:
        return 5, "geo: unspecified"
    if "france" in loc:
        bonus = 8 if config.get("allow_remote_france") else 0
        return bonus, f"geo: other-France mobility ({job.location})"
    return -5, f"geo: outside France ({job.location})"


def score(job: Job, config: dict) -> tuple[int, list[str]]:
    reasons: list[str] = []
    text = _text(job)
    pts = 0

    matched = [k for k in config.get("boost_keywords", []) if k.lower() in text]
    if matched:
        pts += min(50, 12 * len(matched))
        reasons.append(f"keywords: {', '.join(matched[:5])}")

    q = config.get("query", "").lower()
    if q and q in job.title.lower():
        pts += 25
        reasons.append("query in title")
    elif q and all(w in text for w in q.split()):
        pts += 12
        reasons.append("query terms present")

    geo_bonus, geo_reason = _geo_tier(job, config)
    pts += geo_bonus
    reasons.append(geo_reason)

    langs = [l.lower() for l in config.get("languages", [])]
    if not langs or not job.language or job.language.lower() in langs:
        pts += 5
    else:
        pts -= 10
        reasons.append(f"language {job.language} off-target")

    senior_hit = next((t for t in SENIOR_TERMS if t in text), None)
    if senior_hit:
        pts -= 15
        reasons.append(f"seniority signal '{senior_hit}' (soft penalty)")

    return max(0, min(100, pts)), reasons


def evaluate(job: Job, config: dict) -> tuple[int, str, bool]:
    """Returns (score, reasons_text, keep). keep=False for hard-excluded jobs."""
    excl = is_excluded(job, config)
    if excl:
        return 0, excl, False
    pts, reasons = score(job, config)
    keep = pts >= config.get("min_score", 0)
    return pts, "; ".join(reasons), keep
