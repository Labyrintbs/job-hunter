import json

from jobhunter import fetch_diag, match
from jobhunter.sources import free_work


def _listing_html(paths):
    return "".join(f'<a href="{p}">job</a>' for p in paths)


def _detail_html(job_posting=None):
    if job_posting is None:
        return "<html><body>no jobposting here</body></html>"
    return f'<script type="application/ld+json">{json.dumps(job_posting)}</script>'


def _jp(title="Data Scientist", company="Acme", locality="Paris", region="Île-de-France",
        country="FR", employment_type=None, description="<p>Build ML</p>"):
    return {
        "@type": "JobPosting", "title": title,
        "description": description,
        "datePosted": "2026-09-01",
        "employmentType": employment_type or ["FULL_TIME"],
        "hiringOrganization": {"name": company},
        "jobLocation": {"address": {"addressLocality": locality, "addressRegion": region,
                                     "addressCountry": country}},
    }


class Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        pass


class Client:
    """url -> Resp lookup, keyed by exact URL path (listing calls use `params`,
    detail calls don't -- distinguished by whether `params` is passed)."""
    def __init__(self, listing_pages, detail_pages):
        self._listing_pages = list(listing_pages)   # one per successive listing call
        self._detail_pages = detail_pages            # {path: html}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if params is not None:
            html = self._listing_pages.pop(0) if self._listing_pages else ""
            return Resp(html)
        for path, html in self._detail_pages.items():
            if url.endswith(path):
                return Resp(html)
        return Resp("", status_code=404)


def test_fetch_parses_real_job_and_maps_contractor_to_freelance(monkeypatch):
    path = "/fr/tech-it/job-mission/data-scientist/data-scientist-1"
    listing = _listing_html([path])
    detail = _detail_html(_jp(employment_type=["CONTRACTOR"]))
    client = Client([listing], {path: detail})
    monkeypatch.setattr(free_work.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(free_work.time, "sleep", lambda *_: None)

    jobs = free_work.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "free_work" and j.title == "Data Scientist"
    assert j.location == "Paris, Île-de-France"
    assert "Build ML" in j.description and "<p>" not in j.description
    assert j.contract_type == "freelance"


def test_contractor_mapping_makes_exclude_terms_actually_drop_it(monkeypatch):
    # End-to-end: match.py's exclude_terms only recognizes "freelance", not the
    # raw schema.org "CONTRACTOR" value -- confirms the mapping in
    # _contract_type actually closes that gap, not just that the field is set.
    path = "/fr/tech-it/job-mission/data-scientist/data-scientist-1"
    listing = _listing_html([path])
    detail = _detail_html(_jp(employment_type=["CONTRACTOR"]))
    client = Client([listing], {path: detail})
    monkeypatch.setattr(free_work.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(free_work.time, "sleep", lambda *_: None)

    jobs = free_work.fetch("data scientist", max_pages=1, max_detail_fetches=5)
    cfg = {"exclude_terms": ["stage", "freelance", "indépendant", "portage salarial"]}

    assert match.screen(jobs[0], cfg).keep is False


def test_fetch_tracks_non_france_drop(monkeypatch):
    path = "/fr/tech-it/job-mission/data-scientist/data-scientist-1"
    listing = _listing_html([path])
    detail = _detail_html(_jp(locality="Geneva", region="Geneva", country="CH"))
    client = Client([listing], {path: detail})
    monkeypatch.setattr(free_work.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(free_work.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = free_work.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert jobs == []
    assert t.counts[("free_work", "", "non_france")] == 1


def test_fetch_tracks_malformed_record_when_no_jobposting_block(monkeypatch):
    path = "/fr/tech-it/job-mission/data-scientist/data-scientist-1"
    listing = _listing_html([path])
    client = Client([listing], {path: _detail_html(None)})
    monkeypatch.setattr(free_work.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(free_work.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = free_work.fetch("data scientist", max_pages=1, max_detail_fetches=5)

    assert jobs == []
    assert t.counts[("free_work", "", "malformed_record")] == 1


def test_fetch_tracks_pagination_cap_hit_when_more_links_than_cap(monkeypatch):
    paths = [f"/fr/tech-it/job-mission/data-scientist/data-scientist-{i}" for i in range(5)]
    listing = _listing_html(paths)
    detail_pages = {p: _detail_html(_jp(title=f"Job {i}")) for i, p in enumerate(paths)}
    client = Client([listing], detail_pages)
    monkeypatch.setattr(free_work.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(free_work.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = free_work.fetch("data scientist", max_pages=1, max_detail_fetches=2)

    assert len(jobs) == 2   # capped, even though 5 links were found
    assert t.counts[("free_work", "", "pagination_cap_hit")] == 1
