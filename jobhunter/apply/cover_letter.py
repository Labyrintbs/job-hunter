"""Draft a tailored cover letter for a job, grounded in the candidate profile."""
from __future__ import annotations

from pathlib import Path

from ..llm import provider
from ..llm.profile import profile_text
from ..models import Job

SYSTEM = (
    "You write cover letters for a junior ML engineer applying to roles in the Paris area, "
    "following these rules exactly (from templates/cv_tailoring_workflow.md Step 5):\n\n"
    "- Four or five paragraphs, English (unless the posting is in French, then match that).\n"
    "- Open with the most specific connection between the candidate's background and this "
    "role: a matching domain, a matching technique, something in the posting only someone "
    "who read it carefully would pick up on. Never open with \"I am writing to apply for\".\n"
    "- Build the middle around one or two concrete stories with real numbers from the "
    "profile, not a list of skills. The strongest material is usually a diagnosis-and-fix "
    "arc: something broke or underperformed, the candidate found out why, fixed it, here's "
    "the number.\n"
    "- If the posting has an obvious requirement the profile doesn't meet, name that gap "
    "honestly instead of hiding it.\n"
    "- Close with availability (September 2026, Paris) and what specifically draws the "
    "candidate to this company, not a generic closing line.\n"
    "- No em-dashes or en-dashes as sentence connectors, use commas, semicolons, or separate "
    "sentences. Ground every claim in the candidate's real profile below; never invent "
    "experience, employers, or numbers, and never inflate an internship into a full-time "
    "role."
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
    return provider.generate(prompt, system=SYSTEM, max_tokens=1400).strip()


def draft_to_file(job: Job, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = draft(job)
    path = out_dir / "cover_letter.md"
    header = f"# {job.title} — {job.company}\n\n{job.url}\n\n---\n\n"
    path.write_text(header + text + "\n", encoding="utf-8")
    return path
