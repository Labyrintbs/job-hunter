"""LLM-driven choice of which CV entries to keep for one job, and a relevance
score for every bullet and every skills item inside them.

Mirrors templates/cv_tailoring_workflow.md's Step 2 rules: cap Professional
Experience and Projects & Research, default to reverse-chronological order
unless relevance is clearly argued. The LLM does not decide how much fits on
the page: it only ranks. tailor/fit.py builds the full CV and removes the
lowest-scored items until it measurably fits two pages. The LLM only ever
returns ids/indices/scores for the candidate's real, existing content, it
never writes new text, so there is no fabrication risk even when it's wrong
about relevance.

engine.py falls back to deterministic keyword scores when the LLM backend is
unavailable or returns something unusable -- tailoring should never hard-fail
just because this call did.
"""
from __future__ import annotations

from ..llm import provider
from ..models import Job

SYSTEM = (
    "You rank a candidate's REAL, EXISTING CV content for one job posting. You never invent "
    "content and never rewrite wording: you only choose entries by id and score what is "
    "already there.\n\n"
    "Rules:\n"
    "- Professional Experience: keep exactly 2 entries, unless the job clearly calls for "
    "all of them (rare) -- pick whichever are most relevant to this specific job.\n"
    "- Projects & Research: keep exactly 3 entries (or all of them if fewer than 3 exist), "
    "chosen for relevance to this job. A project that's obviously filler for this job "
    "reads worse than a shorter CV, don't pad just to hit 3 if nothing else fits.\n"
    "- Spare projects: also list up to 2 `extra_project_ids`, your next best projects after the "
    "ones you keep, with scores. They are only used if the page turns out to have room.\n"
    "- Order both lists reverse-chronologically (most recent entry first) by default. Only "
    "reorder by relevance if one entry is clearly more relevant to this job than the "
    "others, and say so in `reasoning` -- otherwise keep date order. When two entries are "
    "comparably relevant, prefer the more recent one rather than an older one that happens "
    "to touch the job's domain.\n"
    "- Scores: for every numbered bullet of each kept entry, and every numbered item of each "
    "skills category, give an integer 0-100 for how much it helps THIS application: 100 = "
    "directly what the posting asks for, 50 = useful background, 0-20 = unrelated to this "
    "job. Use the whole range so the weakest items stand out. Do not worry about length or "
    "page space: a separate step removes the lowest-scored items until the CV fits. Return "
    "one score per bullet and per item, in the order given. An entry with no numbered "
    "bullets (description-only) gets an empty list.\n"
    "- Never invent a skill item, a project, an experience, or a bullet that isn't already "
    "given to you."
)

PROMPT = """JOB POSTING:
Title: {title}
Company: {company}
Description:
{description}
{judge_block}
AVAILABLE PROFESSIONAL EXPERIENCE ENTRIES:
{experiences}

AVAILABLE PROJECTS & RESEARCH ENTRIES:
{projects}

AVAILABLE SKILL CATEGORIES (items numbered within each):
{skills}

Return the experience ids to keep (ordered as they should appear) and a same-length list of \
score lists (one score per bullet of that entry, in the same order), the project ids to keep \
(ordered as they should appear) with their score lists, up to 2 spare project ids with their \
score lists, and for each skill category, in the order given, one score per item."""

_SCORES = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "experience_ids": {"type": "array", "items": {"type": "integer"}},
        "experience_scores": _SCORES,
        "project_ids": {"type": "array", "items": {"type": "integer"}},
        "project_scores": _SCORES,
        "extra_project_ids": {"type": "array", "items": {"type": "integer"}},
        "extra_project_scores": _SCORES,
        "skill_scores": _SCORES,
        "reasoning": {"type": "string"},
    },
    "required": ["experience_ids", "experience_scores", "project_ids", "project_scores",
                 "extra_project_ids", "extra_project_scores", "skill_scores", "reasoning"],
}


def _menu(blocks_bullets: list[tuple[str, list[str]]]) -> str:
    """`blocks_bullets` is [(header/full text, [bullet strings])] per block."""
    entries = []
    for i, (text, bullets) in enumerate(blocks_bullets):
        if bullets:
            numbered = "\n".join(f"  bullet {j}: {b.strip()}" for j, b in enumerate(bullets))
            entries.append(f"[{i}]\n{text.strip()}\n{numbered}")
        else:
            entries.append(f"[{i}] (no bullets, description only)\n{text.strip()}")
    return "\n\n".join(entries)


def _skills_menu(skills: list[tuple[str, list[str]]]) -> str:
    return "\n".join(
        f"{name}: " + "; ".join(f"[{j}] {item}" for j, item in enumerate(items))
        for name, items in skills)


def _pinned_block(pinned: dict) -> str:
    parts = []
    if "experiences" in pinned:
        parts.append(f"experience_ids must be exactly {pinned['experiences']} in this order")
    if "projects" in pinned:
        parts.append(f"project_ids must be exactly {pinned['projects']} in this order, and extra_project_ids empty")
    return ("\nREQUIRED CHOICE (fixed by the person, not by you): " + "; ".join(parts)
            + ". Still score every bullet of those entries as usual.\n")


def select(job: Job, experiences: list[tuple[str, list[str]]], projects: list[tuple[str, list[str]]],
           skills: list[tuple[str, list[str]]], judge_context: str | None = None,
           pinned: dict | None = None) -> dict:
    """`experiences`/`projects` are [(block text, [bullet strings])] pairs, see
    engine._menu_pairs; `skills` is [(category name, [item strings])].
    `judge_context` (optional) is the fit-judge's own verdict/reasons for this
    posting, passed through as background (see pipeline._judge_context).
    `pinned` ({"experiences": [ids], "projects": [ids]}, either optional) fixes which entries
    are kept; the call then only scores them."""
    judge_block = f"\nFIT-JUDGE'S OWN ASSESSMENT OF THIS POSTING (background only, don't quote it back):\n{judge_context}\n" if judge_context else ""
    prompt = PROMPT.format(
        title=job.title,
        company=job.company,
        # 16000 matches judge.py/enrich.py's cap -- the full stored JD, not half of it.
        description=(job.description or "")[:16000],
        judge_block=judge_block,
        experiences=_menu(experiences),
        projects=_menu(projects),
        skills=_skills_menu(skills),
    )
    if pinned:
        prompt += _pinned_block(pinned)
    return provider.generate_json(prompt, system=SYSTEM, max_tokens=1500, json_schema=RESULT_SCHEMA,
                                  model=provider.FAST_MODEL, step="cv selection")
