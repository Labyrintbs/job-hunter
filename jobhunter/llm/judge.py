"""LLM fit-judge: score a job against the candidate profile, junior-calibrated."""
from __future__ import annotations

from ..models import Job
from . import provider
from .profile import profile_text

SYSTEM = (
    "You assess how well a specific candidate fits a job posting for their own job "
    "search. Be honest and concrete. The candidate is a junior/new-grad ML engineer "
    "seeking a full-time CDI in the Paris area. Do NOT penalise a posting merely for "
    "asking for 1-3 years of experience; internship, research and project work counts. "
    "Reward genuine ML-engineering relevance; penalise roles that are actually data-"
    "engineering, pure research/PhD, or non-technical."
)

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Rate the fit and return ONLY a JSON object:
{{"score": <int 0-100>, "verdict": "<strong|good|stretch|weak>",
  "seniority": "<junior|mid|senior>", "min_years": <int required years, 0 if none stated>,
  "reasons": "<= 2 sentences>"}}"""


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def judge(job: Job) -> dict:
    prompt = PROMPT.format(
        profile=profile_text()[:6000],
        title=job.title,
        company=job.company,
        location=job.location,
        description=(job.description or "")[:4000],
    )
    data = provider.generate_json(prompt, system=SYSTEM, max_tokens=400)
    score = int(max(0, min(100, data.get("score", 0))))
    return {
        "score": score,
        "verdict": str(data.get("verdict", "")),
        "seniority": str(data.get("seniority", "")),
        "min_years": _int_or_none(data.get("min_years")),
        "reasons": str(data.get("reasons", "")),
    }
