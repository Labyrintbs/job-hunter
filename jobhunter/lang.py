"""French / English detection for job postings and generated text.

Counts common function words of each language (no dependency). English technical
terms inside French text are fine for this: only the glue words are counted.
"""
from __future__ import annotations

import re

_FR = frozenset(
    "le la les des du de un une et est sont pour dans avec sur par au aux nous vous votre vos "
    "notre nos ce cette ces qui que en à il elle ils ont être avoir sera serez rejoindre poste "
    "équipe entreprise missions profil ou où mais donc afin chez vers sans sous".split())
_EN = frozenset(
    "the and of to in for with on is are you your our we will be as an or at by this that from "
    "have has team role experience work join about their who which can into".split())

_TAGS = re.compile(r"<[^>]+>")
_WORDS = re.compile(r"[a-zàâçéèêëîïôûùüÿœ']+")
MIN_HITS = 6          # fewer function words than this: not enough text to decide
DOMINANCE = 1.5       # one language must have this many times the other's hits


def counts(text: str) -> tuple[int, int]:
    """(French, English) function-word hits in the first 6,000 characters."""
    words = _WORDS.findall(_TAGS.sub(" ", text or "").lower()[:6000])
    return sum(w in _FR for w in words), sum(w in _EN for w in words)


def detect(text: str, min_hits: int = MIN_HITS) -> str:
    """"fr", "en", or "" when the text is too short or mixed to tell."""
    fr, en = counts(text)
    if fr + en < min_hits:
        return ""
    if fr > en * DOMINANCE:
        return "fr"
    if en > fr * DOMINANCE:
        return "en"
    return ""


def job_language(title: str, description: str, label: str = "") -> str:
    """The language of a posting: its text first, then its title, then the source's
    own label (unreliable, so last), and English when nothing says otherwise."""
    for found in (detect(description), detect(title, min_hits=2)):
        if found:
            return found
    return label if label in ("fr", "en") else "en"
