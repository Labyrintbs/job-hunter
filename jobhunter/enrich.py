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

_LI_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_LI_MARKUP_RE = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}
_MAX_CHARS = 8000

# Kept as a defense-in-depth safety net against future extraction regressions on
# sites with Stimulus/Turbo-style widget markup (data-controller/data-action,
# analytics hooks) -- the actual leak this was built for turned out to be a bug in
# _strip_html itself (see below), now fixed at the source, so a real posting
# should never trip this anymore.
_LEAK_MARKER_RE = re.compile(
    r"data-(?:controller|action)=|analytics#push|->\w+#|\w+#(?:push|toggle|add|remove|uncheck|expand|collapse)\b"
)
_LEAK_MARKER_THRESHOLD = 8


def _looks_like_scraped_chrome(text: str) -> bool:
    return len(_LEAK_MARKER_RE.findall(text)) >= _LEAK_MARKER_THRESHOLD


# A delisted posting still returns HTTP 200 with a normal-looking page (real nav
# chrome, real page title from the URL slug) -- but the body says the listing was
# taken down, followed by an unrelated "similar postings" carousel. Once
# _strip_html stopped leaking site chrome into "real-looking" text (see above),
# that carousel became clean enough prose to pass every other check and get
# stored as if it were this job's actual description -- confirmed on HelloWork
# job 220 ("Team.is n'est plus disponible"). Catching the takedown notice keeps
# that noise out of scoring/judging.
_DELISTED_RE = re.compile(r"n'est plus disponible|no longer available", re.IGNORECASE)


def _looks_delisted(text: str) -> bool:
    return bool(_DELISTED_RE.search(text))


class _TextExtractor(HTMLParser):
    """Collects visible text, skipping <script>/<style> content."""

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
        if not self._skip_depth and data.strip():
            self.chunks.append(data)


def _strip_html(s: str) -> str:
    """Visible text only, via a real tokenizer rather than a regex.

    A naive `<[^>]+>` tag-strip regex breaks on Stimulus's `data-action`
    syntax (e.g. `data-action="click->toggle#add"`) -- the `->` contains a
    literal `>`, so `[^>]+` ends the "tag" early and everything after it up
    to the real closing `>` leaks into the extracted text as if it were
    visible prose. This is exactly the "scraped chrome" contamination
    _looks_like_scraped_chrome was built to detect after the fact; using
    html.parser.HTMLParser (which tokenizes attributes correctly) avoids the
    leak at the source instead."""
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
    actual description."""
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
            if not text or _looks_like_scraped_chrome(text) or _looks_delisted(text):
                return None
            return text
        if url:
            resp = client.get(url)
            if resp.status_code == 200 and resp.text.strip():
                text = _strip_html(resp.text)[:_MAX_CHARS]
                if not text or _looks_like_scraped_chrome(text) or _looks_delisted(text):
                    return None
                return text
        return None
    except httpx.HTTPError:
        return None
    finally:
        if own:
            client.close()
