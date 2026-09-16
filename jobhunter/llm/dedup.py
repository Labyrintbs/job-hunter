"""LLM duplicate-posting judge: decide whether two heuristically-flagged jobs
(see db.find_possible_duplicates -- same company, same city, near-identical title)
are actually the same real opening, using their full JD text rather than just titles."""
from __future__ import annotations

from ..models import Job
from . import provider

SYSTEM = (
    "You compare two job postings that a heuristic (same company, same city, "
    "near-identical title) has already flagged as possibly the same real opening. "
    "Decide from the actual description text whether they describe the same role, "
    "or two genuinely distinct openings (different team, seniority, specialization, "
    "or an unrelated repost). Be conservative: at the same employer a near-identical "
    "title is often two distinct real roles, not a duplicate -- only call it a "
    "duplicate if the descriptions substantively overlap (same responsibilities, "
    "same team/context, same requirements), not just similar titles."
)

PROMPT = """JOB A
Title: {title_a}
Company: {company_a}
Location: {location_a}
Description:
{description_a}

JOB B
Title: {title_b}
Company: {company_b}
Location: {location_b}
Description:
{description_b}

Return ONLY a JSON object:
{{"verdict": "<same|different>", "confidence": "<high|medium|low>", "reason": "<= 2 sentences>"}}"""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["same", "different"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason"],
}


def compare(job_a: Job, job_b: Job) -> dict:
    prompt = PROMPT.format(
        title_a=job_a.title, company_a=job_a.company, location_a=job_a.location,
        description_a=(job_a.description or "")[:8000],
        title_b=job_b.title, company_b=job_b.company, location_b=job_b.location,
        description_b=(job_b.description or "")[:8000],
    )
    data = provider.generate_json(prompt, system=SYSTEM, max_tokens=300, json_schema=RESULT_SCHEMA)
    return {
        "verdict": str(data.get("verdict", "")),
        "confidence": str(data.get("confidence", "")),
        "reason": str(data.get("reason", "")),
    }
