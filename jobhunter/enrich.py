"""Lazy full-description enrichment.

Search results are often truncated — LinkedIn guest cards carry no description at
all. Rather than pull full pages for every hit (rate-limit / ToS cost), we only
enrich jobs you've actually engaged with (marked interested, or moved past 'new').
The full text is written back onto the job so the judge and the Phase-4 miner have
real content to work with. Read-only; never logs in.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from . import fetch_diag

_LI_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_LI_MARKUP_RE = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
# Header/nav/cookie chrome ahead of the real content can eat the whole _MAX_CHARS
# budget (seen on groupecreditagricole.jobs, real content past char 7000) -- scope
# to <main> when present so that chrome never counts against the budget.
_MAIN_TAG_RE = re.compile(r"<main\b[^>]*>(.*?)</main\s*>", re.S | re.I)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}
_MAX_CHARS = 16000

# The original leak was a _strip_html bug, now fixed at the source (see below) --
# kept as a defense-in-depth net for future extraction regressions on
# Stimulus/Turbo-style widget markup.
_LEAK_MARKER_RE = re.compile(
    r"data-(?:controller|action)=|analytics#push|->\w+#|\w+#(?:push|toggle|add|remove|uncheck|expand|collapse)\b"
)
_LEAK_MARKER_THRESHOLD = 8


def _looks_like_scraped_chrome(text: str) -> bool:
    return len(_LEAK_MARKER_RE.findall(text)) >= _LEAK_MARKER_THRESHOLD


# A delisted posting returns HTTP 200 with real chrome plus a takedown notice
# followed by a "similar postings" carousel clean enough to pass every other
# check (seen on HelloWork job 220) -- catch the notice explicitly.
_DELISTED_RE = re.compile(r"n'est plus disponible|no longer available", re.IGNORECASE)


def _looks_delisted(text: str) -> bool:
    return bool(_DELISTED_RE.search(text))


# Cookie-consent banners can consume the whole _MAX_CHARS budget before the real
# job body is reached (seen on groupecreditagricole.jobs, a 100% boilerplate
# result). Job descriptions never mention cookies, so drop any text node with it.
_COOKIE_BANNER_RE = re.compile(r"\bcookies?\b", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    """Collects visible text, skipping <script>/<style> content and cookie-banner text."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip() and not _COOKIE_BANNER_RE.search(data):
            self.chunks.append(data)


def _strip_html(s: str) -> str:
    """Visible text only, via a real tokenizer rather than a regex.

    A naive `<[^>]+>` regex breaks on Stimulus's `data-action="click->toggle#add"`
    syntax: the embedded `->` ends the "tag" early and leaks everything up to the
    real `>` into the text. html.parser.HTMLParser tokenizes attributes correctly
    and avoids the leak."""
    parser = _TextExtractor()
    parser.feed(s)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()


def fetch_full_text(source: str, external_id: str, url: str,
                    client: httpx.Client | None = None) -> str | None:
    """Best-effort full description text for one job, or None if unavailable --
    including when what came back looks like scraped site chrome (see
    _looks_like_scraped_chrome) or is a takedown notice for a delisted posting
    (see _looks_delisted). Better to leave a job un-enriched than store text
    that's long and clean enough to pass every other check but isn't this job's
    actual description. Each None return is recorded via fetch_diag under an
    `enrich_*` reason."""
    is_linkedin = source == "linkedin" and external_id.isdigit()
    target = _LI_DETAIL_URL.format(id=external_id) if is_linkedin else url
    if not target:
        fetch_diag.track(source, "enrich_no_url", detail=f"external_id={external_id}")
        return None
    own = client is None
    client = client or httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True)
    try:
        resp = client.get(target)
        if resp.status_code != 200 or not resp.text.strip():
            fetch_diag.track(source, "enrich_bad_response", detail=f"HTTP {resp.status_code} {target}")
            return None
        m = (_LI_MARKUP_RE if is_linkedin else _MAIN_TAG_RE).search(resp.text)
        text = _strip_html(m.group(1)) if m else _strip_html(resp.text)
        text = text[:_MAX_CHARS]
        if not text:
            reason = "enrich_empty_text"
        elif _looks_like_scraped_chrome(text):
            reason = "enrich_scraped_chrome"
        elif _looks_delisted(text):
            reason = "enrich_delisted"
        else:
            return text
        fetch_diag.track(source, reason, detail=target)
        return None
    except httpx.HTTPError as exc:
        fetch_diag.track(source, "enrich_network_error", detail=f"{type(exc).__name__} {target}")
        return None
    finally:
        if own:
            client.close()
