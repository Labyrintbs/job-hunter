"""Arbeitnow job board — free public API, no key or login required.

Real pan-European coverage (Berlin, London, Paris, Munich, Hamburg, ... -- verified
live, not Germany-only despite the name), and each record already ships its full
job description, unlike LinkedIn's guest search (title/company/location only). No
server-side keyword search exists -- this paginates the whole feed and leaves
relevance filtering to match.py's screen(), the same way ats.py/hellowork.py
already do for sources without a query param.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone

import httpx

from .. import fetch_diag
from ..models import Job

API_URL = "https://www.arbeitnow.com/api/job-board-api"
THROTTLE_SECONDS = 1.0
# Confirmed live: this API rate-limits (HTTP 429) after roughly a dozen rapid,
# unthrottled requests -- fetch() previously had no throttle or retry at all.


def _strip_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()


def _posted_at(created_at) -> str:
    try:
        return datetime.fromtimestamp(int(created_at), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _to_job(item: dict) -> Job:
    # The API's `remote=true` query param does NOT filter (verified live: identical
    # results with or without it) -- never pass it. Each record's own `remote`
    # boolean is trustworthy though (employer-declared, like wttj's remote:fulltime
    # facet), so location is tagged from that field instead.
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


def _walk(max_pages: int, max_retries: int = 3,
          backoff_base: float = 2.0) -> tuple[list[Job], bool, bool]:
    """Follows links.next for up to max_pages pages, throttled, retrying a 429
    with exponential backoff before giving up on that page. Returns
    (jobs, reached_end, rate_limited) -- reached_end is True once links.next
    is null or a page is empty (the feed's real end); rate_limited is True if
    a page had to be abandoned after exhausting retries (jobs collected before
    that point are still returned, not discarded)."""
    jobs: list[Job] = []
    url: str | None = API_URL
    with httpx.Client(timeout=20.0) as client:
        for i in range(max_pages):
            if not url:
                return jobs, True, False
            if i:
                time.sleep(THROTTLE_SECONDS)
            resp = None
            for attempt in range(max_retries + 1):
                resp = client.get(url)
                if resp.status_code != 429:
                    break
                if attempt < max_retries:
                    time.sleep(backoff_base * 2 ** attempt)
            if resp.status_code == 429:
                return jobs, False, True
            resp.raise_for_status()
            body = resp.json()
            items = body.get("data") or []
            if not items:
                return jobs, True, False
            for item in items:
                if not item.get("slug"):
                    fetch_diag.track("arbeitnow", "malformed_record",
                                      detail=str(item.get("url") or item.get("title") or ""))
                    continue
                jobs.append(_to_job(item))
            url = (body.get("links") or {}).get("next")
    return jobs, url is None, False


def fetch(max_pages: int = 5) -> list[Job]:
    jobs, reached_end, rate_limited = _walk(max_pages)
    if rate_limited:
        fetch_diag.track("arbeitnow", "rate_limited", detail=f"max_pages={max_pages}")
    elif not reached_end:
        fetch_diag.track("arbeitnow", "pagination_cap_hit", detail=f"max_pages={max_pages}")
    return jobs


def fetch_deep(max_pages: int) -> tuple[list[Job], bool, bool]:
    """Backfill entry point (see pipeline.backfill_arbeitnow): same walk as
    fetch(), but more patient about rate-limiting -- a real 429 tends to clear
    within seconds to a couple minutes, not days, so it's simpler and just as
    effective to retry harder within this one run than to defer a cut-short
    walk to the next scheduled day (which would also reopen the page-drift
    problem, just over a shorter gap). Returns (jobs, reached_end, rate_limited)."""
    return _walk(max_pages, max_retries=6, backoff_base=3.0)
