"""Slug-guess whether a company already runs a public ATS board we support.

Triggered once per newly-seen company, only when the LLM judge rates one of
their postings good/strong (see pipeline.judge_one) -- worth the probe cost
only once there's real signal this company is worth pursuing. A guessed slug
can coincidentally collide with an unrelated company's real board on some
ATS, so a hit is NEVER trusted automatically: it's staged as a target_companies
last_result for manual confirmation before anyone adds it to
config/companies.yaml, exactly like any other hand-researched company.

Same URLs/response shapes as sources/ats.py's real fetchers, deliberately --
if the probe says yes, fetch_all must actually agree once the company is
promoted there.
"""
from __future__ import annotations

import re
import time
import unicodedata

import httpx

from .ats import _UA, _is_france

THROTTLE_SECONDS = 0.3

# (ats_type, url_template, response -> list of postings)
_CHECKS = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
     lambda d: d.get("jobs", [])),
    ("lever", "https://api.lever.co/v0/postings/{token}?mode=json",
     lambda d: d if isinstance(d, list) else []),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{token}",
     lambda d: d.get("jobs", [])),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100",
     lambda d: d.get("content", [])),
    ("recruitee", "https://{token}.recruitee.com/api/offers/",
     lambda d: d.get("offers", [])),
    ("workable", "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
     lambda d: d.get("jobs", [])),
]


def _slugs(name: str) -> list[str]:
    """A few plausible ATS token variants for a company display name -- hyphenated,
    concatenated, and just the first word (common for a shortened brand token)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", ascii_name.lower()).split()
    if not words:
        return []
    variants = ["-".join(words), "".join(words), words[0]]
    seen: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.append(v)
    return seen


def _postings_location(ats_type: str, posting: dict) -> str:
    if ats_type == "lever":
        cats = posting.get("categories") or {}
        return cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
    if ats_type == "smartrecruiters":
        loc = posting.get("location") or {}
        return ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
    if ats_type == "recruitee":
        return ", ".join(filter(None, [posting.get("city"), posting.get("country")])) or posting.get("location", "")
    if ats_type == "workable":
        return ", ".join(filter(None, [posting.get("city"), posting.get("country")])) or posting.get("location", "")
    if ats_type == "ashby":
        return posting.get("location", "") or ""
    return (posting.get("location") or {}).get("name", "")  # greenhouse


def probe(company: str, client: httpx.Client | None = None) -> str | None:
    """Try plausible slug variants against each supported ATS's public board
    endpoint. Returns a human-readable, unconfirmed hit description (for
    db.mark_company_checked) on the first board found with at least one
    France-located posting, or None. One caller-visible network call per
    (slug, ats) combo tried -- call this once per company, not per job."""
    own = client is None
    client = client or httpx.Client(timeout=10, headers=_UA)
    try:
        for token in _slugs(company):
            for ats_type, template, extract in _CHECKS:
                try:
                    resp = client.get(template.format(token=token))
                except httpx.HTTPError:
                    continue
                time.sleep(THROTTLE_SECONDS)
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    continue
                postings = extract(data)
                if not postings:
                    continue
                if not any(_is_france(_postings_location(ats_type, p)) for p in postings):
                    continue
                return (f"possible {ats_type} board: token={token}, "
                        f"{len(postings)} postings -- unconfirmed, verify before adding "
                        f"to companies.yaml")
        return None
    finally:
        if own:
            client.close()
