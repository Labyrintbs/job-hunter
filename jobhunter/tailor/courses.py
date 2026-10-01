"""Keep only the Major Modules (Education bullet) that match the job.

Deterministic: each course has a few keywords, the courses sharing the most with
the job text are kept, and they stay in the CV's own order. Reuse-only, like the
rest of the tailor: it only ever drops courses from the existing list.
"""
from __future__ import annotations

import re

KEEP = 4

_MODULES_RE = re.compile(r"(\\textit\{Major Modules\}:\s*)([^}]*)(\})")

# Course name -> job keywords that make it relevant.
_KEYWORDS = {
    "Image Processing": ["image", "vision", "opencv", "segmentation", "detection", "imagerie"],
    "3D Graphics Algorithms": ["3d", "point cloud", "mesh", "rendering", "graphics", "nerf", "slam",
                               "reconstruction", "lidar", "nuage de points"],
    "Data Science": ["data science", "data scientist", "statistic", "analytics", "pandas", "analyse de données"],
    "Computer Architecture": ["embedded", "hardware", "gpu", "cuda", "c++", "performance", "low-level",
                              "systèmes embarqués"],
    "Biomedical Imaging": ["medical", "clinical", "health", "mri", "radiology", "biomedical", "médical",
                           "santé", "imagerie"],
    "Pattern Recognition and Machine Learning for Image Understanding": [
        "machine learning", "deep learning", "classification", "recognition", "apprentissage"],
    "Advanced Methods for Computer Vision/Image Analysis": [
        "computer vision", "image analysis", "vision par ordinateur", "segmentation", "detection"],
}

# Tie-break when the job text says nothing about a course: the broadly useful ones first.
_DEFAULT_ORDER = [
    "Pattern Recognition and Machine Learning for Image Understanding", "Data Science",
    "Advanced Methods for Computer Vision/Image Analysis", "Image Processing",
    "Computer Architecture", "Biomedical Imaging", "3D Graphics Algorithms",
]


def _score(course: str, text: str) -> int:
    return sum(1 for kw in _KEYWORDS.get(course, []) if re.search(r"(?<!\w)" + re.escape(kw), text))


def choose(modules: list[str], job_text: str, keep: int = KEEP) -> list[str]:
    """The `keep` most relevant modules, in their original order."""
    if len(modules) <= keep:
        return list(modules)
    text = job_text.lower()
    rank = {c: i for i, c in enumerate(_DEFAULT_ORDER)}
    ordered = sorted(modules, key=lambda c: (-_score(c, text), rank.get(c, len(rank))))
    kept = set(ordered[:keep])
    return [c for c in modules if c in kept]


def apply(doc: str, job_text: str, keep: int = KEEP) -> str:
    """`doc` with its Major Modules bullet trimmed for this job; unchanged when the
    bullet isn't there."""
    m = _MODULES_RE.search(doc)
    if not m:
        return doc
    modules = [c.strip() for c in m.group(2).split(",") if c.strip()]
    kept = choose(modules, job_text, keep)
    return doc[:m.start(2)] + ", ".join(kept) + doc[m.end(2):]
