"""Helpers for telling whether two listings are the same opening, and which one to keep.

Description overlap is a cheap *signal*, not a verdict: companies reuse one template across
genuinely different roles, so only a near-identical text (SAME_OVERLAP) is trusted without
asking the LLM; anything from CANDIDATE_OVERLAP up is sent to it.
"""
from __future__ import annotations

import re

SHINGLE_WORDS = 5
MIN_CHARS = 300            # shorter descriptions are too thin to compare
CANDIDATE_OVERLAP = 0.3    # same company and at least this similar: worth a check
SAME_OVERLAP = 0.9         # identical text: the same posting, listed more than once

# A job in one of these statuses has been acted on; it is never hidden as a duplicate.
ENGAGED = ("applied", "responded", "interview", "offer", "rejected")
_PROGRESS = {"offer": 5, "interview": 4, "responded": 3, "applied": 2, "rejected": 1,
             "cv_ready": 0.5, "shortlisted": 0.4}
_GEO_RANK = {"idf": 5, "major_city": 4, "france": 3, "remote": 3, "europe_remote": 2, "unknown": 1}


def shingles(text: str | None) -> frozenset[str]:
    words = re.findall(r"\w+", (text or "").lower())
    if len(words) < SHINGLE_WORDS:
        return frozenset()
    return frozenset(" ".join(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1))


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two shingle sets, 0.0 when either is empty."""
    return len(a & b) / len(a | b) if a and b else 0.0


def choose_original(rows: list[dict]) -> int:
    """The id of the listing to keep when `rows` are the same opening. Each row needs id,
    status, geo_tier and fetched_at. A listing you acted on wins (the furthest-along one if
    several); then one with a CV; then the better location; then the newest."""
    def rank(r: dict):
        return (_PROGRESS.get(r["status"], 0), _GEO_RANK.get(r.get("geo_tier") or "", 0),
                r.get("fetched_at") or "", r["id"])
    return max(rows, key=rank)["id"]
