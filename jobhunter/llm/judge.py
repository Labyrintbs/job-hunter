"""LLM fit-judge: score a job against the candidate profile, junior-calibrated."""
from __future__ import annotations

from ..models import Job
from . import provider
from .profile import condensed_profile_text

SYSTEM = (
    "You assess how well a specific candidate fits a job posting for their own job "
    "search. Be honest and concrete. The candidate is a junior/new-grad ML engineer "
    "seeking a full-time CDI in the Paris area, specialized in LLM/NLP model "
    "development (fine-tuning, agentic orchestration, evaluation design) -- not "
    "MLOps/infrastructure, data engineering, or full-stack. Reward genuine LLM/NLP-"
    "application relevance; penalise roles that are actually data engineering, pure "
    "research/PhD, MLOps/infra-heavy (Kubernetes, Terraform, Airflow, feature stores, "
    "CI/CD as the core job, not a supporting skill), sales, marketing, business "
    "development, or otherwise non-technical -- regardless of how many AI/ML keywords "
    "the title or description contains.\n"
    "\n"
    "Seniority: treat a bare years-of-experience requirement as a minor factor, not "
    "an automatic disqualifier, as long as the candidate otherwise fits most of the "
    "role's actual requirements -- internship, research, and project work counts. But "
    "a role requiring proven team-leadership, or titled Staff/Principal/Lead/Head of, "
    "is a real mismatch regardless of the stated years figure; say so.\n"
    "\n"
    "Company type: the candidate wants product companies (SaaS/LegalTech/FinTech/"
    "HealthTech, roughly 10-500 people), not ESNs, staffing firms, or consulting shops "
    "where they'd be placed on client 'missions'. Tells: 'consultant', 'mission', "
    "'chez nos clients', 'accompagner nos clients', 'cabinet de conseil', pre-sales or "
    "forward-deployed/customer-facing duties as a core part of the role (not occasional "
    "contact), headcount in the thousands with offices across 20+ countries. This is a "
    "judgment call, not an automatic fail -- some consultancies have genuinely strong "
    "technical roles; note it, don't reject on the word alone.\n"
    "\n"
    "Hard disqualifiers -- score near 0 regardless of technical fit, and say exactly "
    "which one applies: (1) an explicit citizenship-as-eligibility requirement for a "
    "country the candidate does not hold (they are a Chinese national -- generic "
    "'security clearance' language alone is a softer signal, not this); (2) defense or "
    "dual-use work gated on security clearance, for the same reason; (3) a stated "
    "salary below EUR 39,582 gross/year (2026 Passeport Talent 'salarié qualifié' "
    "threshold)."
)

PROMPT = """CANDIDATE PROFILE:
{profile}
{preferences}
JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Rate the fit and return ONLY a JSON object:
{{"score": <int 0-100>, "verdict": "<strong|good|stretch|weak>",
  "seniority": "<junior|mid|senior>", "min_years": <int required years, 0 if none stated>,
  "reasons": "<= 3 sentences>"}}"""

# Passed as --json-schema to the claude CLI: constrains it to emit a complete, valid
# object matching this shape instead of free-form prose that can get cut off mid-JSON
# on longer (e.g. multi-reason "weak") responses. See provider._generate_cli.
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["strong", "good", "stretch", "weak"]},
        "seniority": {"type": "string", "enum": ["junior", "mid", "senior"]},
        "min_years": {"type": "integer", "minimum": 0},
        "reasons": {"type": "string"},
    },
    "required": ["score", "verdict", "seniority", "min_years", "reasons"],
}


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def judge(job: Job, preferences: str = "") -> dict:
    pref_block = ""
    if preferences:
        pref_block = ("\nLEARNED PREFERENCES (from the candidate's own accept/reject history — "
                      "weigh these):\n" + preferences.strip() + "\n")
    prompt = PROMPT.format(
        profile=condensed_profile_text()[:6000],
        preferences=pref_block,
        title=job.title,
        company=job.company,
        location=job.location,
        # 8000 matches enrich.py's _MAX_CHARS fetch cap -- the full stored JD, not half of it.
        description=(job.description or "")[:8000],
    )
    data = provider.generate_json(prompt, system=SYSTEM, max_tokens=600, json_schema=RESULT_SCHEMA)
    score = int(max(0, min(100, data.get("score", 0))))
    return {
        "score": score,
        "verdict": str(data.get("verdict", "")),
        "seniority": str(data.get("seniority", "")),
        "min_years": _int_or_none(data.get("min_years")),
        "reasons": str(data.get("reasons", "")),
    }
