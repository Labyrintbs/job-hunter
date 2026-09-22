"""Distill an LLM-condensed preference profile from your explicit feedback.

Ground truth is explicit-only: dismissed jobs are negatives, interested jobs are
positives. Claude reads your labeled jobs (title, company, and your own stated
reasons) and writes a short "Prefer .../ Avoid ..." instruction block, versioned
in the DB and injected into the LLM judge (Phase 5). Each update re-derives the
profile from the full current evidence, using the prior version only as a
reference to refine, never appended to or copied unchanged.
"""
from __future__ import annotations

from . import db
from .llm import provider

_PROFILE_SYSTEM = (
    "You distill a job-seeker's preferences from their own accept/reject decisions "
    "into a short instruction block for a downstream job-fit judge. Output 5-8 terse "
    "bullet lines, no preamble. Generalise ONLY from the evidence — never invent a "
    "preference. Capture what to prioritise and what to avoid: domains, seniority, "
    "tech stack, company type. Each bullet starts with 'Prefer' or 'Avoid'. "
    "You may be shown a previous version of this profile as reference — treat it as "
    "your own earlier draft, not as ground truth: re-derive the profile from the "
    "current evidence alone, keep prior bullets only if today's evidence still "
    "supports them, rewrite or drop ones it no longer supports, and add new bullets "
    "the new evidence reveals. Never grow the profile by appending; output a single "
    "revised 5-8 line profile, not a merged or extended list."
)

# Caps one reason string so an outlier (seen: 1,929 chars, a full pasted LLM
# rationale) can't dominate the prompt or crowd out other jobs' evidence.
_MAX_REASON_CHARS = 240


def _example_block(rows, reasons_col: str, limit: int = 100) -> str:
    lines = []
    for r in rows[:limit]:
        reason = (r[reasons_col] or "")[:_MAX_REASON_CHARS]
        tag = f"  [reasons: {reason}]" if reason else ""
        lines.append(f"- {r['title']} @ {r['company']}{tag}")
    return "\n".join(lines) or "(none)"


def condense_profile(conn, max_examples: int = 100, persist: bool = True) -> dict:
    """Ask the LLM to distill a preference profile from labeled jobs. Stored as a
    new version; the latest is injected into the judge in Phase 5. Each call shows
    the LLM the full current evidence (not just what changed) plus, if one exists,
    the previous profile as reference to refine -- never to preserve unconditionally."""
    pos = db.labeled_jobs(conn, "interested")
    neg = db.labeled_jobs(conn, "dismissed")
    if not provider.available():
        return {"status": "no_llm", "interested": len(pos), "dismissed": len(neg)}
    if len(pos) + len(neg) < 3:
        return {"status": "insufficient", "interested": len(pos), "dismissed": len(neg)}

    prior = db.current_profile(conn)
    prior_block = (
        f"YOUR PREVIOUS PROFILE (reference only -- refine, don't just repeat or "
        f"append; keep only what today's evidence below still supports):\n{prior['text']}\n\n"
        if prior else ""
    )
    prompt = (
        f"{prior_block}"
        f"INTERESTED (you liked these):\n{_example_block(pos, 'interested_reasons', limit=max_examples)}\n\n"
        f"DISMISSED (you rejected these):\n{_example_block(neg, 'dismiss_reasons', limit=max_examples)}\n\n"
        "Write the preference profile now."
    )
    text = provider.generate(prompt, system=_PROFILE_SYSTEM, max_tokens=400).strip()
    if persist and text:
        db.add_profile(conn, text, len(pos), len(neg))
    return {"status": "ok", "text": text, "interested": len(pos), "dismissed": len(neg)}
