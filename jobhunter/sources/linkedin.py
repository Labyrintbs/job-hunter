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

from .. import fetch_diag
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


def _job_url(card: str, job_id: str) -> str:
    """The card's own scraped href when the regex finds it (has the real slug);
    else LinkedIn's canonical /jobs/view/{id}/, which resolves the same posting
    from the id alone. _LINK_RE occasionally misses -- it requires the class
    attribute before href in the same tag, not guaranteed for every card variant
    -- so fall back rather than leave the job with no clickable link."""
    m = _LINK_RE.search(card)
    if m:
        return m.group(1)
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


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
        url=_job_url(card, urn.group(1)),
        posted_at=(_TIME_RE.search(card).group(1) if _TIME_RE.search(card) else ""),
    )


def _fetch_one(client: httpx.Client, query: str, location: str, max_pages: int,
                recent_hours: int, max_retries: int, backoff_base: float,
                workplace_type: str | None = None) -> list[Job]:  # LinkedIn's f_WT param, e.g. "2"=remote -- see fetch()'s docstring
    jobs: list[Job] = []
    for page in range(max_pages):
        params = {
            "keywords": query,
            "location": location,
            "start": page * 10,
            "f_TPR": f"r{recent_hours * 3600}",
        }
        if workplace_type:
            params["f_WT"] = workplace_type
        url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
        resp = None
        for attempt in range(max_retries + 1):
            resp = client.get(url)
            if resp.status_code != 429:
                break
            if attempt < max_retries:
                time.sleep(backoff_base * 2 ** attempt)
        if resp.status_code != 200 or not resp.text.strip():
            fetch_diag.track("linkedin", "page_fetch_stopped",
                              detail=f"{query!r}@{location!r} page={page} status={resp.status_code}")
            break  # rate-limited past retries, or exhausted
        cards = _CARD_RE.findall(resp.text)
        if not cards:
            break
        parsed = [_parse_card(c) for c in cards]
        n_malformed = sum(1 for p in parsed if p is None)
        if n_malformed:
            fetch_diag.track("linkedin", "malformed_card",
                              detail=f"{n_malformed} of {len(cards)} cards, {query!r}@{location!r}")
        jobs.extend(p for p in parsed if p)
        time.sleep(THROTTLE_SECONDS)
    else:
        fetch_diag.track("linkedin", "pagination_cap_hit", detail=f"{query!r}@{location!r} max_pages={max_pages}")
    return jobs


def fetch(queries: list[str], locations: list[str], max_pages: int = 5,
          recent_hours: int = 168, max_retries: int = 3,
          backoff_base: float = 2.0, workplace_type: str | None = None) -> list[Job]:
    """One request per (query, location) pair's page; a 429 retries just that page,
    any other failure only breaks that pair's own pagination. Cross-pair duplicates
    are harmless -- db.py's dedup collapses them.

    workplace_type is LinkedIn's own f_WT filter, used only as a coarse pre-filter,
    not a trusted remote signal (confirmed live: it lets non-remote postings
    through) -- location is returned untouched, and genuine remote status is
    confirmed later from full JD text (see match.detect_remote_from_text,
    pipeline.enrich_one)."""
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_HEADERS) as client:
        for query in queries:
            for location in locations:
                jobs.extend(_fetch_one(client, query, location, max_pages,
                                        recent_hours, max_retries, backoff_base,
                                        workplace_type))
    return jobs
