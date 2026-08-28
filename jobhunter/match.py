"""Rule-based screening for a Paris junior ML-engineer hunt.

Two-stage triage:
  * keep     — store it at all (ML-relevant by title, not a hard-excluded intern/blocklist).
  * filtered — stored but auto-hidden into the Filtered bucket (senior title, too many
               years required, or below min_score). Never deleted, so false-negatives
               stay reviewable and can be rescued.

Seniority is a hard bucket gate here (not just a soft penalty) because obvious
senior/lead/staff titles are the main noise for a junior search — but a junior/new-grad
title always wins, so a "5 years" mention in a junior posting can't push it out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job

# Senior markers (FR+EN). Title hit => senior bucket; description-only hit => ranking penalty.
SENIOR_TERMS = [
    "senior", "sr.", "lead", "principal", "staff", "head of", "head of ai",
    "confirmé", "confirme", "expert", "director", "directeur", "vp ", "vice president",
    "expérimenté", "experimente", "manager",
]
# Junior markers always protect a posting from the seniority gate.
JUNIOR_TERMS = [
    "junior", "new grad", "new-grad", "graduate", "entry level", "entry-level",
    "débutant", "debutant", "jeune diplômé", "jeune diplome", "associate",
]

# A years-of-experience requirement, only when tied to an experience/expérience context
# (so "founded 8 years ago" is not read as "requires 8 years").
_EXP_YEARS_RE = re.compile(
    r"(?:(\d{1,2})\s*\+?\s*(?:-|–|to|à|au)?\s*\d{0,2}\s*"
    r"(?:years?|yrs?|ans|années?|annees?)[^.\n]{0,40}?(?:experience|expérience|experiences?|exp\b|d['’]exp)"
    r"|(?:experience|expérience|minimum|at least|au moins)[^.\n]{0,40}?"
    r"(\d{1,2})\s*\+?\s*(?:-|–|to|à)?\s*\d{0,2}\s*(?:years?|yrs?|ans|années?|annees?))",
    re.IGNORECASE,
)


def _text(job: Job) -> str:
    return f"{job.title} {job.contract_type} {job.description}".lower()


def min_years_required(text: str) -> int | None:
    """Lowest experience-in-years floor found in the text, or None. Conservative: takes
    the minimum across mentions and ignores absurd values (>15, likely company age)."""
    nums: list[int] = []
    for m in _EXP_YEARS_RE.finditer(text):
        n = m.group(1) or m.group(2)
        if n:
            nums.append(int(n))
    nums = [n for n in nums if 0 < n <= 15]
    return min(nums) if nums else None


def detect_seniority(job: Job) -> str:
    """junior | senior | unknown. Title junior markers win outright."""
    title = job.title.lower()
    if any(t in title for t in JUNIOR_TERMS):
        return "junior"
    if any(t in title for t in SENIOR_TERMS):
        return "senior"
    return "unknown"


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
        reasons.append(f"seniority signal '{senior_hit.strip()}'")

    return max(0, min(100, pts)), reasons


def is_relevant(job: Job, config: dict) -> bool:
    """A job must be an ML role by its TITLE, not just be in Paris or mention ML
    in company boilerplate. Guards against non-ML roles from ATS boards."""
    title = job.title.lower()
    role_kw = config.get("role_keywords") or config.get("boost_keywords", [])
    if any(k.lower() in title for k in role_kw):
        return True
    q = config.get("query", "").lower()
    return bool(q) and all(w in title for w in q.split())


@dataclass
class Screening:
    score: int
    reasons: str
    keep: bool
    filtered: bool
    filter_reason: str
    seniority: str
    min_years: int | None


def screen(job: Job, config: dict) -> Screening:
    """Triage one job. keep=False => not stored (excluded / not ML-relevant).
    filtered=True => stored but auto-hidden (senior / too many years / below min_score)."""
    excl = is_excluded(job, config)
    if excl:
        return Screening(0, excl, keep=False, filtered=False, filter_reason=excl,
                         seniority="unknown", min_years=None)
    if not is_relevant(job, config):
        return Screening(0, "not ML-relevant", keep=False, filtered=False,
                         filter_reason="not ML-relevant", seniority="unknown", min_years=None)

    pts, reasons = score(job, config)
    seniority = detect_seniority(job)
    min_years = min_years_required(_text(job))

    sen_cfg = config.get("seniority") or {}
    gate = sen_cfg.get("filter", True)
    max_years = sen_cfg.get("max_years", 3)

    flags: list[str] = []
    if gate and seniority == "senior":
        flags.append("senior title")
    if gate and seniority != "junior" and min_years is not None and min_years > max_years:
        flags.append(f"requires {min_years}+ yrs")
    if pts < config.get("min_score", 0):
        flags.append(f"score<{config.get('min_score', 0)}")

    return Screening(
        score=pts,
        reasons="; ".join(reasons),
        keep=True,
        filtered=bool(flags),
        filter_reason="; ".join(flags),
        seniority=seniority,
        min_years=min_years,
    )


def evaluate(job: Job, config: dict) -> tuple[int, str, bool]:
    """Back-compat shim: (score, reasons, keep-and-not-filtered)."""
    s = screen(job, config)
    return s.score, (s.filter_reason if not s.keep else s.reasons), s.keep and not s.filtered
