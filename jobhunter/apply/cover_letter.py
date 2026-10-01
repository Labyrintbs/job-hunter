"""Draft a tailored cover letter for a job, grounded in the candidate profile."""
from __future__ import annotations

from pathlib import Path

from .. import fetch_diag
from ..lang import counts
from ..llm import provider
from ..llm.profile import profile_text
from ..models import Job

_LANGUAGE = {
    "en": "English",
    "fr": ("French (the whole letter in French, in a professional register with the polite "
           "\"vous\" form and a standard French closing formula; keep only standard technical "
           "terms in English, as French ML job posts do, e.g. fine-tuning, LLM-as-a-judge, pipeline)"),
}

SYSTEM = (
    "You write cover letters for a junior ML engineer applying to roles in the Paris area, "
    "following these rules exactly (from templates/cv_tailoring_workflow.md Step 5):\n\n"
    "- Four or five paragraphs, written in {language}.\n"
    "- Open with the most specific connection between the candidate's background and this "
    "role: a matching domain, a matching technique, something in the posting only someone "
    "who read it carefully would pick up on. Never open with \"I am writing to apply for\".\n"
    "- Build the middle around one or two concrete stories with real numbers from the "
    "profile, not a list of skills. The strongest material is usually a diagnosis-and-fix "
    "arc: something broke or underperformed, the candidate found out why, fixed it, here's "
    "the number.\n"
    "- If the posting has an obvious requirement the profile doesn't meet, name that gap "
    "honestly instead of hiding it.\n"
    "- Close with availability (available immediately, Paris) and what specifically draws "
    "the candidate to this company, not a generic closing line.\n"
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
{judge_block}
Write the cover letter body only (no address block, no placeholders like [Name])."""


def language_problem(text: str, language: str) -> str:
    """"" when the letter reads in `language`, else why not. Function words only,
    so English technical terms in a French letter don't count."""
    fr_hits, en_hits = counts(text)
    mine, other = (fr_hits, en_hits) if language == "fr" else (en_hits, fr_hits)
    if mine >= 4 * other:
        return ""
    return f"the letter reads as {'EN' if language == 'fr' else 'FR'} ({other} vs {mine} function words)"


def draft(job: Job, judge_context: str | None = None, cv_text: str | None = None,
          language: str = "en", feedback: str = "") -> str:
    """`judge_context` (optional) is the fit-judge's own verdict/reasons for this
    posting, passed through as background (see pipeline._judge_context). `cv_text`
    (optional) is the tailored CV sent with this application; without it the letter
    is grounded in the full base CV. `language` is the letter's language (the CV's);
    `feedback` is why a previous attempt was rejected."""
    judge_block = f"\nFIT-JUDGE'S OWN ASSESSMENT OF THIS POSTING (background only, don't quote it back):\n{judge_context}\n" if judge_context else ""
    if feedback:
        judge_block += (f"\nYour previous attempt was rejected: {feedback}. Write the whole letter in "
                        f"{'French' if language == 'fr' else 'English'}.\n")
    profile = profile_text(language)
    if cv_text:
        profile = ("(This is the exact CV sent with this application; refer only to what it "
                   "contains.)\n" + cv_text)
    prompt = PROMPT.format(
        profile=profile,
        title=job.title,
        company=job.company,
        # 16000 matches judge.py/select.py/enrich.py's cap -- the full stored JD.
        description=(job.description or "")[:16000],
        judge_block=judge_block,
    )
    system = SYSTEM.format(language=_LANGUAGE.get(language, _LANGUAGE["en"]))
    return provider.generate(prompt, system=system, max_tokens=1400).strip()


def draft_to_file(job: Job, out_dir: Path, judge_context: str | None = None,
                  cv_text: str | None = None, language: str = "en") -> Path:
    """Write the letter; a wrong-language draft is retried once, and one that is still
    wrong is kept but tracked (cover_language_mismatch) so it is not sent unseen."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text = draft(job, judge_context=judge_context, cv_text=cv_text, language=language)
    problem = language_problem(text, language)
    if problem:
        text = draft(job, judge_context=judge_context, cv_text=cv_text, language=language, feedback=problem)
        problem = language_problem(text, language)
        if problem:
            fetch_diag.track("cover", "cover_language_mismatch", detail=problem, company=job.company)
    path = out_dir / "cover_letter.md"
    header = f"# {job.title} — {job.company}\n\n{job.url}\n\n---\n\n"
    path.write_text(header + text + "\n", encoding="utf-8")
    return path
