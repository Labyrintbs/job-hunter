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

# Some sites' pages are mostly client-side widget markup (Stimulus/Turbo-style
# data-controller/data-action attributes, analytics hooks, cookie-banner JS) that
# a plain tag-strip doesn't fully remove -- it leaks through as if it were visible
# prose and crowds the real posting text out of the _MAX_CHARS budget (confirmed
# on a HelloWork listing: description_full=1 but the stored text was almost
# entirely nav/account-menu/analytics chrome, with the real job text truncated
# away near the end). A real job posting essentially never contains even one of
# these tokens, so a handful is a reliable "this isn't real content" signal.
_LEAK_MARKER_RE = re.compile(
    r"data-(?:controller|action)=|analytics#push|->\w+#|\w+#(?:push|toggle|add|remove|uncheck|expand|collapse)\b"
)
_LEAK_MARKER_THRESHOLD = 8


def _looks_like_scraped_chrome(text: str) -> bool:
    return len(_LEAK_MARKER_RE.findall(text)) >= _LEAK_MARKER_THRESHOLD


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_full_text(source: str, external_id: str, url: str,
                    client: httpx.Client | None = None) -> str | None:
    """Best-effort full description text for one job, or None if unavailable
    (including when what came back looks like scraped site chrome rather than a
    real posting, see _looks_like_scraped_chrome -- better to leave a job
    un-enriched than store garbage that's long enough to pass the "real content"
    length gate elsewhere but isn't real content)."""
    own = client is None
    client = client or httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True)
    try:
        if source == "linkedin" and external_id.isdigit():
            resp = client.get(_LI_DETAIL_URL.format(id=external_id))
            if resp.status_code != 200 or not resp.text.strip():
                return None
            m = _LI_MARKUP_RE.search(resp.text)
            text = _strip_html(m.group(1)) if m else _strip_html(resp.text)
            text = text[:_MAX_CHARS]
            return text if text and not _looks_like_scraped_chrome(text) else None
        if url:
            resp = client.get(url)
            if resp.status_code == 200 and resp.text.strip():
                text = _strip_html(resp.text)[:_MAX_CHARS]
                return text if text and not _looks_like_scraped_chrome(text) else None
        return None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            client.close()
