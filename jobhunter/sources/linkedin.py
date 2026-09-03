"""LinkedIn jobs via the public guest search endpoint (no login).

This uses the same unauthenticated `jobs-guest` endpoint that the site serves to
logged-out visitors — the least ToS-hostile way to read LinkedIn postings. It is
still rate-limited: we throttle, cap pages, and retry a 429 with backoff before
giving up on that page. fetch() covers multiple query/location pairs for more
volume; each pair's pagination is independent so one bad combo doesn't cost the
others. For richer data or higher volume still you would need a logged-in session
(secondary account) and accept higher ban risk — deliberately out of scope here.

Read-only: this never logs in and never applies.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse

import httpx

from ..models import Job

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
THROTTLE_SECONDS = 1.5  # gentle; guest endpoint 429s easily
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}

_CARD_RE = re.compile(r"<li>(.*?)</li>", re.S)
_URN_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_LINK_RE = re.compile(r'base-card__full-link[^>]*href="([^"?]+)')
_TITLE_RE = re.compile(r'base-search-card__title[^>]*>(.*?)</h3>', re.S)
_COMPANY_RE = re.compile(r'base-search-card__subtitle.*?>(.*?)</', re.S)
_LOCATION_RE = re.compile(r'job-search-card__location[^>]*>(.*?)</span>', re.S)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]*)"')


def _text(m: re.Match | None) -> str:
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", m.group(1))).strip()


def _parse_card(card: str) -> Job | None:
    urn = _URN_RE.search(card)
    if not urn:
        return None
    return Job(
        source="linkedin",
        external_id=urn.group(1),
        title=_text(_TITLE_RE.search(card)),
        company=_text(_COMPANY_RE.search(card)),
        location=_text(_LOCATION_RE.search(card)),
        url=(_LINK_RE.search(card).group(1) if _LINK_RE.search(card) else ""),
        posted_at=(_TIME_RE.search(card).group(1) if _TIME_RE.search(card) else ""),
    )


def _fetch_one(client: httpx.Client, query: str, location: str, max_pages: int,
                recent_hours: int, max_retries: int, backoff_base: float) -> list[Job]:
    jobs: list[Job] = []
    for page in range(max_pages):
        params = {
            "keywords": query,
            "location": location,
            "start": page * 10,
            "f_TPR": f"r{recent_hours * 3600}",
        }
        url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
        resp = None
        for attempt in range(max_retries + 1):
            resp = client.get(url)
            if resp.status_code != 429:
                break
            if attempt < max_retries:
                time.sleep(backoff_base * 2 ** attempt)
        if resp.status_code != 200 or not resp.text.strip():
            break  # rate-limited past retries, or exhausted
        cards = _CARD_RE.findall(resp.text)
        if not cards:
            break
        jobs.extend(j for j in (_parse_card(c) for c in cards) if j)
        time.sleep(THROTTLE_SECONDS)
    return jobs


def fetch(queries: list[str], locations: list[str], max_pages: int = 5,
          recent_hours: int = 168, max_retries: int = 3,
          backoff_base: float = 2.0) -> list[Job]:
    """One request per (query, location) pair's page. A 429 retries just that page
    with exponential backoff instead of abandoning the whole fetch; any other
    non-200/empty response or exhausted results only breaks that pair's pagination,
    so one bad combo doesn't cost the others. Cross-pair duplicates are harmless --
    external_id is stable regardless of which search surfaced the posting, and
    db.py's dedup collapses them."""
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        for query in queries:
            for location in locations:
                jobs.extend(_fetch_one(client, query, location, max_pages,
                                        recent_hours, max_retries, backoff_base))
    return jobs
