"""LLM-driven selection of which CV blocks (and which bullets within them) to
keep for one job.

Mirrors templates/cv_tailoring_workflow.md's Step 2 rules (the same rules
applied when tailoring by hand in conversation): cap Professional Experience
and Projects & Research, default to reverse-chronological order unless
relevance is clearly argued, trim bullets that don't earn their space, keep
Skills reuse-only. The LLM only ever chooses IDs/indices/names from a fixed
menu of the candidate's real, existing content, it never writes new bullets or
invents an entry, so there is no fabrication risk even when it's wrong about
relevance.

engine.py falls back to a deterministic keyword-overlap heuristic (whole
blocks, no bullet trimming) when the LLM backend is unavailable or returns
something unusable -- tailoring should never hard-fail just because this call
did.
"""
from __future__ import annotations

from ..llm import provider
from ..models import Job

SYSTEM = (
    "You tailor a CV by choosing which of the candidate's REAL, EXISTING entries and "
    "bullets to keep for one job posting. You never invent content and never rewrite a "
    "bullet's wording, you only select and order from the ids/indices/names given to you.\n\n"
    "Rules:\n"
    "- Professional Experience: keep exactly 2 entries, unless the job clearly calls for "
    "all of them (rare) -- pick whichever are most relevant to this specific job.\n"
    "- Projects & Research: keep exactly 3 entries (or all of them if fewer than 3 exist), "
    "chosen for relevance to this job. A project that's obviously filler for this job "
    "reads worse than a shorter CV, don't pad just to hit 3 if nothing else fits.\n"
    "- Order both lists reverse-chronologically (most recent entry first) by default. Only "
    "reorder by relevance if one entry is clearly more relevant to this job than the "
    "others, and say so in `reasoning` -- otherwise keep date order. When two entries are "
    "comparably relevant, prefer the more recent one rather than an older one that happens "
    "to touch the job's domain.\n"
    "- Bullets: for each kept entry, also choose which of its own numbered bullets to keep. "
    "Drop a bullet that doesn't earn its space for this specific job (redundant with "
    "another kept bullet, or clearly the least relevant of the set) rather than keeping "
    "every bullet by default. A entry with no numbered bullets (description-only) has "
    "nothing to choose, return an empty list for it.\n"
    "- Aim for a CV that fills its second page well without a large empty area at the "
    "bottom, favor keeping slightly more bullets over leaving a page visibly sparse, but "
    "never re-add a bullet you'd otherwise cut just to fill space -- a full page of "
    "genuinely relevant content beats a full page of filler.\n"
    "- Skills: keep every category unless it is clearly irrelevant to this job (e.g. "
    "medical-imaging skills for a role with no vision/health angle at all). When genuinely "
    "unsure, keep it, dropping a relevant category is worse than keeping an irrelevant one.\n"
    "- Never invent a skill item, a project, an experience, or a bullet that isn't already "
    "given to you."
)

PROMPT = """JOB POSTING:
Title: {title}
Company: {company}
Description:
{description}
{feedback_block}
AVAILABLE PROFESSIONAL EXPERIENCE ENTRIES:
{experiences}

AVAILABLE PROJECTS & RESEARCH ENTRIES:
{projects}

AVAILABLE SKILL CATEGORIES:
{skills}

Return the experience ids to keep (ordered as they should appear), a same-length list of \
bullet-index lists (one per kept experience, in the same order), the project ids to keep \
(ordered as they should appear), a same-length list of bullet-index lists for those \
projects, and the skill category names to keep."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "experience_ids": {"type": "array", "items": {"type": "integer"}},
        "experience_bullets": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "project_ids": {"type": "array", "items": {"type": "integer"}},
        "project_bullets": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "skill_categories": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["experience_ids", "experience_bullets", "project_ids", "project_bullets",
                 "skill_categories", "reasoning"],
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


def select(job: Job, experiences: list[tuple[str, list[str]]], projects: list[tuple[str, list[str]]],
           skill_names: list[str], feedback: str | None = None) -> dict:
    """`experiences`/`projects` are [(block text, [bullet strings])] pairs, see
    engine._menu_pairs. `feedback` (optional) is a hint from a previous attempt
    that didn't fit the page, e.g. "compiled to 3 pages, trim more content"."""
    feedback_block = f"\nNOTE: {feedback}\n" if feedback else "\n"
    prompt = PROMPT.format(
        title=job.title,
        company=job.company,
        # 8000 matches judge.py/enrich.py's cap -- the full stored JD, not half of it.
        description=(job.description or "")[:8000],
        feedback_block=feedback_block,
        experiences=_menu(experiences),
        projects=_menu(projects),
        skills="\n".join(f"- {n}" for n in skill_names),
    )
    return provider.generate_json(prompt, system=SYSTEM, max_tokens=1000, json_schema=RESULT_SCHEMA)
