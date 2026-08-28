"""Lazy full-description enrichment.

Search results are often truncated — LinkedIn guest cards carry no description at
all. Rather than pull full pages for every hit (rate-limit / ToS cost), we only
enrich jobs you've actually engaged with (marked interested, or moved past 'new').
The full text is written back onto the job so the judge and the Phase-4 miner have
real content to work with. Read-only; never logs in.
"""
from __future__ import annotations

import html
import re

import httpx

_LI_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_LI_MARKUP_RE = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}
_MAX_CHARS = 8000


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_full_text(source: str, external_id: str, url: str,
                    client: httpx.Client | None = None) -> str | None:
    """Best-effort full description text for one job, or None if unavailable."""
    own = client is None
    client = client or httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True)
    try:
        if source == "linkedin" and external_id.isdigit():
            resp = client.get(_LI_DETAIL_URL.format(id=external_id))
            if resp.status_code != 200 or not resp.text.strip():
                return None
            m = _LI_MARKUP_RE.search(resp.text)
            text = _strip_html(m.group(1)) if m else _strip_html(resp.text)
            return text[:_MAX_CHARS] or None
        if url:
            resp = client.get(url)
            if resp.status_code == 200 and resp.text.strip():
                return _strip_html(resp.text)[:_MAX_CHARS] or None
        return None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            client.close()
