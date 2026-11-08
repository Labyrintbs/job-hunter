"""Draft a tailored cover letter for a job, grounded in the candidate profile.

Three steps: analyse (requirements -> CV evidence plan), draft (the letter, in the
candidate's voice), check (length, stock phrases, numbers not in the CV) with one retry.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from .. import fetch_diag
from ..lang import counts
from ..llm import provider
from ..llm.profile import profile_text
from ..models import Job
from ..sources.ats import _UA, _strip_html

VOICE_FILE = Path(__file__).resolve().parents[2] / "templates" / "cover_letter_voice.md"
MAX_WORDS = 380   # the prompt asks for 250-350; a little slack before a retry

_LANGUAGE = {
    "en": "English",
    "fr": ("French (the whole letter in French, in a professional register with the polite "
           "\"vous\" form and a standard French closing formula; keep only standard technical "
           "terms in English, as French ML job posts do, e.g. fine-tuning, LLM-as-a-judge, pipeline)"),
}

# Phrases that fit any letter; a letter containing one is retried (check()).
STOCK_PHRASES = (
    "i am writing to apply", "i am passionate", "passionate about", "fast-paced", "dynamic team",
    "innovative company", "team player", "je vous écris pour", "je me permets", "passionné par",
    "équipe dynamique", "entreprise innovante", "esprit d'équipe",
)

SYSTEM = (
    "You write cover letters for a junior ML engineer applying to roles in France. Rules:\n\n"
    "- Three or four short paragraphs, 250 to 350 words, written in {language}.\n"
    "- Open with the most specific connection between the candidate's background and this "
    "role, using the posting's own technical terms. Never open with \"I am writing to apply\".\n"
    "- Build the middle around ONE concrete story with real numbers (a second only in one "
    "sentence). Do not restate the CV's bullets; say what the work showed that this role needs. "
    "A diagnosis-and-fix arc is strongest: something broke, the candidate found out why, fixed "
    "it, here's the number.\n"
    "- If the posting has an obvious requirement the profile doesn't meet, name that gap "
    "honestly in one or two sentences instead of hiding it.\n"
    "- Say why this company, with something specific from the posting or the company text "
    "(its product, stack or problem). No line that could fit another company.\n"
    "- Close briefly with availability (available immediately; based in Paris, and when the job "
    "is in another French city, open to relocating there).\n"
    "- Plain words and short sentences, like the voice samples. No stock phrases (passionate "
    "about, dynamic team, innovative company), no em-dashes or en-dashes as sentence connectors.\n"
    "- Ground every claim in the candidate's real profile below; never invent experience, "
    "employers, or numbers, and never inflate an internship into a full-time role. Don't state "
    "durations or dates unless the profile gives them."
)

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}
{plan_block}{judge_block}
Write the cover letter body only (no address block, no placeholders like [Name])."""

ANALYSIS_SYSTEM = (
    "You plan a cover letter. From the posting and the candidate's CV, pick the posting's three "
    "most important needs, and for each the single best matching item in the CV (quote the CV "
    "line it comes from). Only use what the CV actually says."
)
ANALYSIS_PROMPT = """CANDIDATE CV:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Description:
{description}
{company_block}
Return JSON: needs (up to 3: need in the posting's own words, evidence from the CV, cv_line quoted \
from the CV), company_hook (one specific thing about this company from the posting or company \
text; empty if none), gap (the most obvious requirement the CV does not meet; empty if none)."""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "needs": {"type": "array", "items": {"type": "object", "properties": {
            "need": {"type": "string"}, "evidence": {"type": "string"}, "cv_line": {"type": "string"}},
            "required": ["need", "evidence", "cv_line"]}},
        "company_hook": {"type": "string"},
        "gap": {"type": "string"},
    },
    "required": ["needs", "company_hook", "gap"],
}


def voice_block() -> str:
    try:
        text = VOICE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return f"\nVOICE SAMPLES (the candidate's own writing; match this register):\n{text}\n" if text else ""


def fetch_company_text(url: str) -> str:
    """Plain text of the company's homepage; '' on any failure (the letter just has less to use)."""
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers=_UA)
        r.raise_for_status()
        return _strip_html(re.sub(r"(?s)<(script|style).*?</\1>", " ", r.text))[:3000]
    except Exception:
        return ""


def analyse(job: Job, profile: str, company_text: str = "") -> dict:
    """Requirements -> evidence plan; {} when the model's answer can't be used (the draft
    then works from the posting alone, as before)."""
    company_block = f"\nCOMPANY TEXT (from its own site):\n{company_text}\n" if company_text else ""
    prompt = ANALYSIS_PROMPT.format(profile=profile, title=job.title, company=job.company,
                                    description=(job.description or "")[:16000], company_block=company_block)
    try:
        return provider.generate_json(prompt, system=ANALYSIS_SYSTEM, json_schema=ANALYSIS_SCHEMA,
                                      max_tokens=900, step="cover plan")
    except provider.LLMUnavailable:
        raise
    except Exception:
        return {}


def _plan_block(plan: dict | None) -> str:
    if not plan or not plan.get("needs"):
        return ""
    lines = ["\nLETTER PLAN (from the posting and CV; follow it):"]
    for i, n in enumerate(plan["needs"][:3], 1):
        lines.append(f"{i}. need: {n.get('need', '')} | evidence: {n.get('evidence', '')} "
                     f"| CV line: {n.get('cv_line', '')}")
    if plan.get("company_hook"):
        lines.append(f"Why this company: {plan['company_hook']}")
    if plan.get("gap"):
        lines.append(f"Gap to name honestly: {plan['gap']}")
    return "\n".join(lines) + "\n"


def language_problem(text: str, language: str) -> str:
    """"" when the letter reads in `language`, else why not. Function words only,
    so English technical terms in a French letter don't count."""
    fr_hits, en_hits = counts(text)
    mine, other = (fr_hits, en_hits) if language == "fr" else (en_hits, fr_hits)
    if mine >= 4 * other:
        return ""
    return f"the letter reads as {'EN' if language == 'fr' else 'FR'} ({other} vs {mine} function words)"


def _numbers(text: str) -> set[str]:
    """Comparable forms of the figures in `text` (3 000 / 3,000 -> 3000; 96,8 -> 96.8);
    single-digit-to-12 counts and years are ignored."""
    out = set()
    for m in re.findall(r"\d[\d\s  .,]*\d|\d", text):
        s = re.sub(r"[\s  ]", "", m).strip(".,")
        s = re.sub(r"[.,]", "", s) if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s) else s.replace(",", ".")
        if re.fullmatch(r"\d{1,2}", s) and int(s) <= 12 or re.fullmatch(r"(19|20)\d\d", s):
            continue
        out.add(s)
    return out


def problems(text: str, source_text: str, language: str) -> list[str]:
    """Why this letter should be redone: language, length, dashes, stock phrases, figures
    that are in neither the CV nor the posting (`source_text` is both)."""
    found = []
    if lang := language_problem(text, language):
        found.append(lang)
    words = len(text.split())
    if words > MAX_WORDS:
        found.append(f"{words} words, keep it between 250 and 350")
    if "—" in text or "–" in text:
        found.append("no em-dashes or en-dashes")
    low = text.lower()
    if stock := [p for p in STOCK_PHRASES if p in low]:
        found.append("stock phrases to rewrite: " + ", ".join(stock))
    if bad := sorted(_numbers(text) - _numbers(source_text)):
        found.append("figures not in the CV or posting, remove them: " + ", ".join(bad))
    return found


def draft(job: Job, judge_context: str | None = None, cv_text: str | None = None,
          language: str = "en", feedback: str = "", plan: dict | None = None) -> str:
    """`judge_context` (optional) is the fit-judge's own verdict/reasons for this
    posting, passed through as background (see pipeline._judge_context). `cv_text`
    (optional) is the tailored CV sent with this application; without it the letter
    is grounded in the full base CV. `language` is the letter's language (the CV's);
    `feedback` is why a previous attempt was rejected; `plan` comes from analyse()."""
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
        location=job.location or "not stated",
        # 16000 matches judge.py/select.py/enrich.py's cap -- the full stored JD.
        description=(job.description or "")[:16000],
        plan_block=_plan_block(plan),
        judge_block=judge_block,
    )
    system = SYSTEM.format(language=_LANGUAGE.get(language, _LANGUAGE["en"])) + voice_block()
    return provider.generate(prompt, system=system, max_tokens=1000, step="cover letter").strip()


def draft_to_file(job: Job, out_dir: Path, judge_context: str | None = None,
                  cv_text: str | None = None, language: str = "en", company_text: str = "") -> Path:
    """Write the letter. A draft that fails problems() is retried once with those reasons;
    one that still fails is kept but tracked (cover_language_mismatch / cover_check_failed)
    so it is not sent unseen."""
    out_dir.mkdir(parents=True, exist_ok=True)
    source = cv_text or profile_text(language)
    plan = analyse(job, source, company_text)
    kw = dict(judge_context=judge_context, cv_text=cv_text, language=language, plan=plan)
    check_source = source + "\n" + (job.description or "")
    text = draft(job, **kw)
    found = problems(text, check_source, language)
    if found:
        retry = draft(job, feedback="; ".join(found), **kw)
        retry_found = problems(retry, check_source, language)
        if len(retry_found) <= len(found):   # keep the better of the two
            text, found = retry, retry_found
    if found:
        lang_issue = [f for f in found if f.startswith("the letter reads as")]
        fetch_diag.track("cover", "cover_language_mismatch" if lang_issue else "cover_check_failed",
                         detail="; ".join(found), company=job.company)
    path = out_dir / "cover_letter.md"
    header = f"# {job.title} — {job.company}\n\n{job.url}\n\n---\n\n"
    path.write_text(header + text + "\n", encoding="utf-8")
    return path
