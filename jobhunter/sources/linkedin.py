"""LinkedIn jobs via the public guest search endpoint (no login).

This uses the same unauthenticated `jobs-guest` endpoint that the site serves to
logged-out visitors — the least ToS-hostile way to read LinkedIn postings. It is
still rate-limited: we throttle, cap pages, and back off on non-200. For richer
data or higher volume you would need a logged-in session (secondary account) and
accept higher ban risk — deliberately out of scope here.

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


def fetch(query: str, location: str = "Paris, France", max_pages: int = 3,
          recent_hours: int = 168) -> list[Job]:
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        for page in range(max_pages):
            params = {
                "keywords": query,
                "location": location,
                "start": page * 10,
                "f_TPR": f"r{recent_hours * 3600}",
            }
            url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
            resp = client.get(url)
            if resp.status_code != 200 or not resp.text.strip():
                break  # rate-limited or exhausted
            cards = _CARD_RE.findall(resp.text)
            if not cards:
                break
            jobs.extend(j for j in (_parse_card(c) for c in cards) if j)
            time.sleep(THROTTLE_SECONDS)
    return jobs
