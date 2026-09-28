"""artificialintelligencejobs.co -- public JSON API, no key, no auth.

Niche AI/ML-focused job board (documented params at its own /developers
page). No description field at all -- same shallow shape as LinkedIn, relies
on enrich.py's lazy full-description pass. `apply_url` (when present)
usually points at the real originating ATS posting rather than this site's
own thin page, so it's stored as the job's url -- a strictly better
enrichment target.

Genuinely recency-sorted (unlike eures/free_work/lesjeudis), and volume for
this project's target queries is small enough that max_hits comfortably
covers everything that exists -- no backfill needed here.
"""
from __future__ import annotations

import time

import httpx

from .. import fetch_diag
from ..models import Job
from .ats import _UA, _is_france

SEARCH_URL = "https://artificialintelligencejobs.co/api/jobs"
THROTTLE_SECONDS = 0.3
PAGE_SIZE = 200


def fetch(query: str, max_hits: int = 100, region: str = "europe",
          country_only: bool = True) -> list[Job]:
    jobs: list[Job] = []
    offset = 0
    matched = 0
    with httpx.Client(timeout=20, headers=_UA) as c:
        while len(jobs) < max_hits:
            resp = c.get(SEARCH_URL, params={"q": query, "region": region,
                                              "limit": PAGE_SIZE, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            matched = data.get("matched", 0)
            listings = data.get("jobs", [])
            if not listings:
                break
            for j in listings:
                company = j.get("company", "") or ""
                location = (j.get("location") or "").strip()
                if j.get("remote") and "remote" not in location.lower():
                    location = f"{location} - Remote" if location else "Remote"
                if country_only and not _is_france(location):
                    fetch_diag.track("aijobs", "non_france", detail=location, company=company)
                    continue
                jobs.append(Job(
                    source="aijobs",
                    external_id=j.get("url", "") or j.get("apply_url", ""),
                    title=(j.get("title") or "").strip(),
                    company=company,
                    location=location,
                    url=j.get("apply_url") or j.get("url", "") or "",
                    posted_at=j.get("posted", "") or "",
                ))
            offset += PAGE_SIZE
            time.sleep(THROTTLE_SECONDS)
            if len(listings) < PAGE_SIZE:
                break
    if len(jobs) >= max_hits and offset < matched:
        fetch_diag.track("aijobs", "pagination_cap_hit", detail=f"{query!r}: matched={matched} max_hits={max_hits}")
    return jobs[:max_hits]
