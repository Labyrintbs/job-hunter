"""Welcome to the Jungle fetch via its public, search-only Algolia backend.

WTTJ ships a public, referer-restricted Algolia search key in its frontend; the
jobs index is `wk_cms_jobs_production`. No login or bot wall. The key only works
with a welcometothejungle.com `Referer` header (that's the access control), and
the per-index `/query` endpoint. We throttle politely and page to respect
Algolia's 1000-hit-per-query cap.
"""
from __future__ import annotations

import json
import time

import httpx

from ..models import Job

APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wk_cms_jobs_production"
QUERY_URL = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/{INDEX}/query"

HITS_PER_PAGE = 30
THROTTLE_SECONDS = 0.3  # stay well under ~4 req/s

_HEADERS = {
    "x-algolia-api-key": API_KEY,
    "x-algolia-application-id": APP_ID,
    "referer": "https://www.welcometothejungle.com/",
    "content-type": "application/x-www-form-urlencoded",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
_AGENT = {"x-algolia-agent": "Algolia for JavaScript (4.26.0); Browser"}


def _job_url(hit: dict) -> str:
    org_slug = (hit.get("organization") or {}).get("slug", "")
    slug = hit.get("slug", "")
    if org_slug and slug:
        return f"https://www.welcometothejungle.com/en/companies/{org_slug}/jobs/{slug}"
    return ""


def _location(hit: dict) -> str:
    offices = hit.get("offices") or []
    if not offices:
        return ""
    o = offices[0]
    parts = [o.get("city"), o.get("state"), o.get("country")]
    return ", ".join(p for p in parts if p)


def _to_job(hit: dict) -> Job:
    return Job(
        source="wttj",
        external_id=str(hit.get("objectID") or hit.get("reference") or hit.get("slug")),
        title=hit.get("name", "") or "",
        company=(hit.get("organization") or {}).get("name", "") or "",
        location=_location(hit),
        language=hit.get("language", "") or "",
        url=_job_url(hit),
        description=(hit.get("profile") or "")[:5000],
        contract_type=hit.get("contract_type", "") or "",
        posted_at=hit.get("published_at", "") or "",
    )


def fetch(query: str, max_hits: int = 100, country: str = "France") -> list[Job]:
    jobs: list[Job] = []
    page = 0
    with httpx.Client(timeout=20.0) as client:
        while len(jobs) < max_hits:
            body: dict[str, object] = {
                "query": query,
                "hitsPerPage": HITS_PER_PAGE,
                "page": page,
            }
            if country:
                body["facetFilters"] = [[f"offices.country:{country}"]]
            resp = client.post(
                QUERY_URL, params=_AGENT, headers=_HEADERS, content=json.dumps(body)
            )
            resp.raise_for_status()
            result = resp.json()
            hits = result.get("hits", [])
            if not hits:
                break
            jobs.extend(_to_job(h) for h in hits)
            page += 1
            if page >= result.get("nbPages", 1):
                break
            time.sleep(THROTTLE_SECONDS)
    return jobs[:max_hits]
