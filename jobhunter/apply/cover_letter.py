"""Draft a tailored cover letter for a job, grounded in the candidate profile."""
from __future__ import annotations

from pathlib import Path

from ..llm import provider
from ..llm.profile import profile_text
from ..models import Job

SYSTEM = (
    "You write concise, specific cover letters for a junior ML engineer applying to "
    "roles in the Paris area. Ground every claim in the candidate's real profile; never "
    "invent experience, employers, or numbers. Plain, confident, no clichés. About 250 "
    "words, 3 short paragraphs. Match the posting's language (French or English)."
)

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Description:
{description}

Write the cover letter body only (no address block, no placeholders like [Name])."""


def draft(job: Job) -> str:
    prompt = PROMPT.format(
        profile=profile_text()[:6000],
        title=job.title,
        company=job.company,
        description=(job.description or "")[:4000],
    )
    return provider.generate(prompt, system=SYSTEM, max_tokens=900).strip()


def draft_to_file(job: Job, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = draft(job)
    path = out_dir / "cover_letter.md"
    header = f"# {job.title} — {job.company}\n\n{job.url}\n\n---\n\n"
    path.write_text(header + text + "\n", encoding="utf-8")
    return path
