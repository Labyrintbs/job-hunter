"""lesjeudis.com -- plain server-rendered HTML listing + per-job schema.org
JobPosting JSON-LD, no API, no key.

robots.txt is broadly permissive (`Allow: /`), blocking only account/dashboard
paths. Search is `/emploi/<slugified-query>?page=N` (its documented
`/jobs?search=` URL now redirects here), with real pagination (distinct link
sets page to page).

Same real-depth-but-not-recency-sorted shape as Free-Work -- capped at
max_detail_fetches per run, no backfill for this source (see pipeline.py).
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .. import fetch_diag
from ..models import Job
from .ats import _UA, _is_france, _strip_html

BASE = "https://lesjeudis.com"
THROTTLE_SECONDS = 0.3
_LINK_RE = re.compile(r"/offers/[a-z0-9-]+")
_JSONLD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(query: str) -> str:
    return _SLUG_RE.sub("-", query.lower()).strip("-")


def _job_posting(html: str) -> dict | None:
    for block in _JSONLD_RE.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def fetch(query: str, max_pages: int = 3, max_detail_fetches: int = 45,
          country_only: bool = True) -> list[Job]:
    jobs: list[Job] = []
    links: list[str] = []
    listing_url = f"{BASE}/emploi/{_slugify(query)}"
    with httpx.Client(timeout=20, headers=_UA) as c:
        for page in range(1, max_pages + 1):
            resp = c.get(listing_url, params={"page": page} if page > 1 else None)
            if resp.status_code != 200:
                break
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
                fetch_diag.track("lesjeudis", "malformed_record", detail=path)
                continue
            address = (jp.get("jobLocation") or {}).get("address") or {}
            location = ", ".join(filter(None, [address.get("addressLocality"), address.get("postalCode")]))
            if country_only and not _is_france(location, address.get("addressCountry", "")):
                fetch_diag.track("lesjeudis", "non_france", detail=location)
                continue
            identifier = (jp.get("identifier") or {}).get("value", "") or path
            employment_type = jp.get("employmentType") or ""
            if isinstance(employment_type, list):
                employment_type = ", ".join(employment_type)
            jobs.append(Job(
                source="lesjeudis",
                external_id=identifier,
                title=(jp.get("title") or "").strip(),
                company=(jp.get("hiringOrganization") or {}).get("name", "") or "",
                location=location,
                url=jp.get("url") or (BASE + path),
                description=_strip_html(jp.get("description", "") or "")[:5000],
                contract_type=employment_type,
                posted_at=jp.get("datePosted", "") or "",
            ))
            time.sleep(THROTTLE_SECONDS)

    if len(links) > max_detail_fetches:
        fetch_diag.track("lesjeudis", "pagination_cap_hit",
                          detail=f"{query!r}: {len(links)} links found, only {max_detail_fetches} detail-fetched")
    return jobs
