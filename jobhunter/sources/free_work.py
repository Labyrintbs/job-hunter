"""Free-Work (free-work.com) -- plain server-rendered HTML listing + per-job
schema.org JobPosting JSON-LD, no API, no key.

robots.txt only disallows /login, /logout, /fw-deals -- job pages untouched.
Listing pages are server-rendered (no JS needed) with real pagination. Each
job's detail page carries a complete JobPosting block including salary --
rare among French-language sources.

Real per-query depth exceeds a shallow poll, but ranking isn't recency-sorted
either, so (like HelloWork) a deeper periodic walk wouldn't reliably buy
freshness -- capped at max_detail_fetches per run instead. No backfill for
this source (see pipeline.py).
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .. import fetch_diag
from ..models import Job
from .ats import _UA, _is_france, _strip_html

BASE = "https://www.free-work.com"
LISTING_URL = f"{BASE}/fr/tech-it/jobs"
THROTTLE_SECONDS = 0.3
_LINK_RE = re.compile(r"/fr/tech-it/job-mission/[a-z0-9-]+/[a-z0-9-]+")
_JSONLD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)


def _job_posting(html: str) -> dict | None:
    for block in _JSONLD_RE.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _contract_type(jp: dict) -> str:
    types = jp.get("employmentType") or []
    if isinstance(types, str):
        types = [types]
    # This site's freelance/mission postings carry schema.org's "CONTRACTOR" value,
    # which match.py's exclude_terms ("freelance"/"indépendant"/etc) wouldn't match --
    # normalize it to a term the existing filter already recognizes.
    if any(t in ("CONTRACTOR", "TEMPORARY") for t in types):
        return "freelance"
    return ", ".join(types)


def fetch(query: str, max_pages: int = 3, max_detail_fetches: int = 45,
          country_only: bool = True) -> list[Job]:
    jobs: list[Job] = []
    links: list[str] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        for page in range(1, max_pages + 1):
            resp = c.get(LISTING_URL, params={"query": query, "page": page})
            resp.raise_for_status()
            new_links = [l for l in dict.fromkeys(_LINK_RE.findall(resp.text)) if l not in links]
            if not new_links:
                break
            links.extend(new_links)
            time.sleep(THROTTLE_SECONDS)

        for path in links[:max_detail_fetches]:
            resp = c.get(BASE + path)
            if resp.status_code != 200:
                continue
            jp = _job_posting(resp.text)
            if not jp or not jp.get("title"):
                fetch_diag.track("free_work", "malformed_record", detail=path)
                continue
            address = (jp.get("jobLocation") or {}).get("address") or {}
            locality, region = address.get("addressLocality"), address.get("addressRegion")
            location = ", ".join(dict.fromkeys(filter(None, [locality, region])))
            if country_only and not _is_france(location, address.get("addressCountry", "")):
                fetch_diag.track("free_work", "non_france", detail=location)
                continue
            jobs.append(Job(
                source="free_work",
                external_id=path,
                title=(jp.get("title") or "").strip(),
                company=(jp.get("hiringOrganization") or {}).get("name", "") or "",
                location=location,
                url=BASE + path,
                description=_strip_html(jp.get("description", "") or "")[:5000],
                contract_type=_contract_type(jp),
                posted_at=jp.get("datePosted", "") or "",
            ))
            time.sleep(THROTTLE_SECONDS)

    if len(links) > max_detail_fetches:
        fetch_diag.track("free_work", "pagination_cap_hit",
                          detail=f"{query!r}: {len(links)} links found, only {max_detail_fetches} detail-fetched")
    return jobs
