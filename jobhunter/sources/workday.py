"""Workday CXS API -- public JSON, no auth required.

Workday-hosted career sites (common among large French/European corporates:
Airbus, Renault, Valeo, ...) render their job search client-side, but the
underlying search and job-detail endpoints are plain unauthenticated JSON,
just like Greenhouse/Lever/Ashby. The catch is there's no single global
board id -- each company has its own (tenant, wd_host, site) triple, visible
in its career page's URL or embedded in that page's HTML (e.g.
"valeo.wd3.myworkdayjobs.com/en-EN/valeo_jobs" -> tenant=valeo, wd_host=wd3,
site=valeo_jobs). Two calls per matched posting: the search endpoint lists
title/location only, the detail endpoint returns the full HTML description.
"""
from __future__ import annotations

import time

import httpx

from ..models import Job
from .ats import THROTTLE_SECONDS, _UA, _is_france, _strip_html

PAGE_SIZE = 20

# Fallback only -- production always passes config/search.yaml's workday.queries
# instead (see pipeline.py), which also covers computer vision/NLP/LLM/PM.
DEFAULT_QUERIES = ["machine learning", "data scientist", "artificial intelligence",
                   "deep learning", "algorithm engineer"]


def fetch(tenant: str, wd_host: str, site: str, company: str, locale: str = "en-US",
          queries: list[str] | None = None, pages_per_query: int = 2) -> list[Job]:
    base = f"https://{tenant}.{wd_host}.myworkdayjobs.com"
    search_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    queries = queries or DEFAULT_QUERIES
    seen_paths: set[str] = set()
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        for q in queries:
            offset = 0
            for _ in range(pages_per_query):
                resp = c.post(search_url, json={"appliedFacets": {}, "limit": PAGE_SIZE,
                                                 "offset": offset, "searchText": q})
                resp.raise_for_status()
                data = resp.json()
                postings = data.get("jobPostings", [])
                if not postings:
                    break
                for p in postings:
                    path = p.get("externalPath", "")
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)
                    # locationsText is the normal field, but some tenants (e.g. Renault's
                    # site) leave it empty and put the city in bulletFields[0] instead --
                    # harmless to fall back to it even where it's actually a req id, since
                    # that just won't match any France hint.
                    location = (p.get("locationsText") or "").strip()
                    if not location:
                        bullets = p.get("bulletFields") or [""]
                        location = bullets[0] or ""
                    if not _is_france(location):
                        continue
                    description, req_id = "", ""
                    try:
                        detail = c.get(f"{base}/wday/cxs/{tenant}/{site}{path}")
                        detail.raise_for_status()
                        info = detail.json().get("jobPostingInfo", {})
                        description = _strip_html(info.get("jobDescription", ""))[:5000]
                        req_id = info.get("jobReqId", "") or ""
                    except httpx.HTTPError:
                        pass
                    jobs.append(Job(
                        source="workday",
                        external_id=req_id or path,
                        title=(p.get("title") or "").strip(),
                        company=company,
                        location=location,
                        url=f"{base}/{locale}/{site}{path}",
                        description=description,
                        posted_at=p.get("postedOn", "") or "",
                    ))
                    time.sleep(THROTTLE_SECONDS)
                offset += PAGE_SIZE
                if offset >= data.get("total", 0):
                    break
    return jobs
