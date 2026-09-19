"""Arbeitnow job board — free public API, no key or login required.

Real pan-European coverage (Berlin, London, Paris, Munich, Hamburg, ... -- verified
live, not Germany-only despite the name), and each record already ships its full
job description, unlike LinkedIn's guest search (title/company/location only).

No server-side keyword search exists (confirmed against the docs and live) -- this
paginates the whole feed and leaves relevance filtering to match.py's screen(), the
same way ats.py/hellowork.py already do for sources without a query param.

The API's own `remote=true` query param does NOT actually filter (verified live:
identical results with or without it) -- never pass it. Each job record's own
`remote` boolean field is trustworthy though (employer-declared via their own
"Remote" tag, same category of signal as wttj's `remote:fulltime` facet, not a
search-side promise) -- fetch() tags the location from that field.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import httpx

from ..models import Job

API_URL = "https://www.arbeitnow.com/api/job-board-api"


def _strip_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()


def _posted_at(created_at) -> str:
    try:
        return datetime.fromtimestamp(int(created_at), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _to_job(item: dict) -> Job:
    location = (item.get("location") or "").strip()
    if item.get("remote") and "remote" not in location.lower():
        location = f"{location} - Remote" if location else "Remote"
    return Job(
        source="arbeitnow",
        external_id=str(item.get("slug") or item.get("url") or ""),
        title=item.get("title", "") or "",
        company=item.get("company_name", "") or "",
        location=location,
        url=item.get("url", "") or "",
        description=_strip_html(item.get("description", "")),
        contract_type=", ".join(item.get("job_types") or []),
        posted_at=_posted_at(item.get("created_at")),
    )


def fetch(max_pages: int = 5) -> list[Job]:
    jobs: list[Job] = []
    url: str | None = API_URL
    with httpx.Client(timeout=20.0) as client:
        for _ in range(max_pages):
            if not url:
                break
            resp = client.get(url)
            resp.raise_for_status()
            body = resp.json()
            items = body.get("data") or []
            if not items:
                break
            jobs.extend(_to_job(item) for item in items if item.get("slug"))
            url = (body.get("links") or {}).get("next")
    return jobs
