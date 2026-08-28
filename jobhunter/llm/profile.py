"""Plain-text profile extracted from the base CV, used as LLM context."""
from __future__ import annotations

import re
from functools import lru_cache

from ..tailor.engine import BASE_CV


@lru_cache(maxsize=1)
def profile_text() -> str:
    raw = BASE_CV.read_text(encoding="utf-8")
    body = raw.split(r"\begin{document}")[-1].split(r"\end{document}")[0]
    # drop comments and the macro-heavy layout, keep human-readable content
    body = re.sub(r"(?m)%.*$", "", body)
    body = re.sub(r"\\resume[A-Za-z]+|\\section|\\textbf|\\textit|\\textnormal|\\href|\\small|\\Huge|\\scshape|\\item|\\projectdesc|\\vspace\{[^}]*\}|\\faLinkedin|\\raisebox\{[^}]*\}", " ", body)
    body = re.sub(r"\\[A-Za-z]+", " ", body)          # any remaining commands
    body = re.sub(r"[{}\\$&~^]|\[[^\]]*\]", " ", body)  # braces/specials/optional args
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()
