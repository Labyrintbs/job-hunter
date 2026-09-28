import json

from jobhunter import fetch_diag
from jobhunter.sources import lesjeudis


def _listing_html(paths):
    return "".join(f'<a href="{p}">job</a>' for p in paths)


def _detail_html(job_posting=None):
    if job_posting is None:
        return "<html><body>no jobposting here</body></html>"
    return f'<script type="application/ld+json">{json.dumps(job_posting)}</script>'


def _jp(title="Data Scientist", company="Acme", locality="Lyon", postal="69001",
        country="FR", identifier="uuid-1", description="Build ML models"):
    return {
        "@type": "JobPosting", "title": title,
        "description": description,
        "datePosted": "2026-09-01",
        "hiringOrganization": {"name": company},
        "url": f"https://lesjeudis.com/offers/{title.lower().replace(' ', '-')}",
        "identifier": {"@type": "PropertyValue", "name": "LesJeudis", "value": identifier},
        "jobLocation": {"address": {"addressLocality": locality, "postalCode": postal,
                                     "addressCountry": country}},
    }


class Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class Client:
    """Keyed by exact URL: the fixed listing URL vs. individual /offers/... detail paths."""
    def __init__(self, listing_url, listing_pages, detail_pages):
        self._listing_url = listing_url
        self._listing_pages = list(listing_pages)
        self._detail_pages = detail_pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if url == self._listing_url:
            html = self._listing_pages.pop(0) if self._listing_pages else ""
            return Resp(html)
        for path, html in self._detail_pages.items():
            if url.endswith(path):
                return Resp(html)
        return Resp("", status_code=404)


def test_slugify_lowercases_and_hyphenates_query():
    assert lesjeudis._slugify("Machine Learning Engineer") == "machine-learning-engineer"
    assert lesjeudis._slugify("data scientist") == "data-scientist"


def test_fetch_parses_real_job(monkeypatch):
    path = "/offers/data-scientist-abc123"
    listing_url = f"{lesjeudis.BASE}/emploi/data-scientist"
    client = Client(listing_url, [_listing_html([path])], {path: _detail_html(_jp())})
    monkeypatch.setattr(lesjeudis.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(lesjeudis.time, "sleep", lambda *_: None)

    jobs = lesjeudis.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "lesjeudis" and j.title == "Data Scientist"
    assert j.external_id == "uuid-1"   # prefers identifier.value over the URL path
    assert j.location == "Lyon, 69001"
    assert j.description == "Build ML models"


def test_fetch_tracks_non_france_drop(monkeypatch):
    path = "/offers/data-scientist-abc123"
    listing_url = f"{lesjeudis.BASE}/emploi/data-scientist"
    detail = _detail_html(_jp(locality="Geneva", postal="1200", country="CH"))
    client = Client(listing_url, [_listing_html([path])], {path: detail})
    monkeypatch.setattr(lesjeudis.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(lesjeudis.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = lesjeudis.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert jobs == []
    assert t.counts[("lesjeudis", "", "non_france")] == 1


def test_fetch_tracks_malformed_record(monkeypatch):
    path = "/offers/data-scientist-abc123"
    listing_url = f"{lesjeudis.BASE}/emploi/data-scientist"
    client = Client(listing_url, [_listing_html([path])], {path: _detail_html(None)})
    monkeypatch.setattr(lesjeudis.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(lesjeudis.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = lesjeudis.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert jobs == []
    assert t.counts[("lesjeudis", "", "malformed_record")] == 1


def test_fetch_tracks_pagination_cap_hit_when_more_links_than_cap(monkeypatch):
    paths = [f"/offers/data-scientist-{i}" for i in range(5)]
    listing_url = f"{lesjeudis.BASE}/emploi/data-scientist"
    detail_pages = {p: _detail_html(_jp(title=f"Job {i}", identifier=f"uuid-{i}")) for i, p in enumerate(paths)}
    client = Client(listing_url, [_listing_html(paths)], detail_pages)
    monkeypatch.setattr(lesjeudis.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(lesjeudis.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = lesjeudis.fetch("data scientist", max_pages=1, max_detail_fetches=2)

    assert len(jobs) == 2
    assert t.counts[("lesjeudis", "", "pagination_cap_hit")] == 1
