"""Plain-text profile extracted from the base CV, used as LLM context."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..tailor.engine import BASE_CV


def _clean_latex(body: str) -> str:
    """Drop comments and the macro-heavy layout, keep human-readable content.

    Escaped percent signs (\\%, real content -- an actual percentage) are protected
    before comment-stripping, which otherwise treats any bare '%' as a comment marker
    and silently truncates the rest of that line, even the part after \\% that isn't
    a comment at all.
    """
    body = body.replace(r"\%", "\uE000")
    body = re.sub(r"(?m)%.*$", "", body)
    body = body.replace("\uE000", "%")
    body = re.sub(r"\\resume[A-Za-z]+|\\section|\\textbf|\\textit|\\textnormal|\\href|\\small|\\Huge|\\scshape|\\item|\\projectdesc|\\vspace\{[^}]*\}|\\faLinkedin|\\raisebox\{[^}]*\}", " ", body)
    body = re.sub(r"\\[A-Za-z]+", " ", body)          # any remaining commands
    body = re.sub(r"[{}\\$&~^]|\[[^\]]*\]", " ", body)  # braces/specials/optional args
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()


def _body_of(raw: str) -> str:
    return raw.split(r"\begin{document}")[-1].split(r"\end{document}")[0]


def _document_body() -> str:
    return _body_of(BASE_CV.read_text(encoding="utf-8"))


def tailored_cv_text(tex_path: Path) -> str:
    """Plain text of one tailored CV's .tex, cleaned the same way as profile_text()."""
    return _clean_latex(_body_of(tex_path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def profile_text() -> str:
    return _clean_latex(_document_body())

