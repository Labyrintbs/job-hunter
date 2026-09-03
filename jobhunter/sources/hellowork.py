"""HelloWork job search — page-1 only, deliberately narrow.

No public JSON API exists (confirmed: plain server-rendered HTML, no __NEXT_DATA__/
Algolia/Meilisearch trace). robots.txt disallows the search page
(/fr-fr/emploi/recherche.html) AND every query-string URL sitewide for User-agent: *
-- a stronger explicit disallow than LinkedIn's guest-endpoint precedent in this repo.
We respect that by only ever issuing ONE request per (query, location): no pagination
is attempted. That request also only returns the first ~30 of however many total
results exist server-side; the rest is gated behind a JS-triggered "load more"
(Stimulus intersect controller) with no plain-HTTP fallback URL, so it's out of reach
without a headless browser -- deliberately out of scope here.

Read-only: this never logs in and never applies.
"""
from __future__ import annotations

import html
import re
import urllib.parse

import httpx

from ..models import Job

SEARCH_URL = "https://www.hellowork.com/fr-fr/emploi/recherche.html"
THROTTLE_SECONDS = 1.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

_CARD_SPLIT = 'data-cy="serpCard"'
_ID_RE = re.compile(r'href="/fr-fr/emplois/(\d+)\.html"')
_TITLE_RE = re.compile(r'<p class="typo-l[^"]*">(.*?)</p>', re.S)
_COMPANY_RE = re.compile(r'<p class="typo-s inline">(.*?)</p>', re.S)
_LOCATION_RE = re.compile(r'data-cy="localisationCard"[^>]*>\s*([^<]+?)\s*<', re.S)
_CONTRACT_RE = re.compile(r'data-cy="contractCard"[^>]*>\s*([^<]+?)\s*<', re.S)


def _text(m: re.Match | None) -> str:
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", m.group(1))).strip()


def _parse_card(card: str) -> Job | None:
    m = _ID_RE.search(card)
    if not m:
        return None
    return Job(
        source="hellowork",
        external_id=m.group(1),
        title=_text(_TITLE_RE.search(card)),
        company=_text(_COMPANY_RE.search(card)),
        location=_text(_LOCATION_RE.search(card)),
        url=f"https://www.hellowork.com/fr-fr/emplois/{m.group(1)}.html",
        contract_type=_text(_CONTRACT_RE.search(card)),
    )


def fetch(query: str, location: str = "Paris") -> list[Job]:
    params = {"k": query, "l": location}
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    resp = httpx.get(url, headers=_HEADERS, timeout=20)
    if resp.status_code != 200:
        return []
    cards = resp.text.split(_CARD_SPLIT)[1:]  # [0] is everything before the first card
    return [j for j in (_parse_card(c) for c in cards) if j]
