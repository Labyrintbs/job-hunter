from jobhunter import fetch_diag
from jobhunter.sources import ats, workday


class Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class Client:
    """Fakes httpx.Client for workday's two-call flow: POST to search, GET for detail.
    search_payloads is consumed one-per-call (one per page/query); detail_payloads is
    keyed by the job's externalPath."""

    def __init__(self, search_payloads, detail_payloads):
        self._search = list(search_payloads)
        self._detail = detail_payloads
        self.posts = []
        self.gets = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self.posts.append((url, json))
        payload = self._search.pop(0) if self._search else {"total": 0, "jobPostings": []}
        return Resp(payload)

    def get(self, url):
        self.gets.append(url)
        for path, payload in self._detail.items():
            if url.endswith(path):
                return Resp(payload)
        return Resp({"jobPostingInfo": {}})


def _patch(monkeypatch, search_payloads, detail_payloads=None):
    client = Client(search_payloads, detail_payloads or {})
    monkeypatch.setattr(workday.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(workday.time, "sleep", lambda *_: None)
    return client


def test_fetch_filters_to_france_and_pulls_full_description(monkeypatch):
    search_page = {
        "total": 2,
        "jobPostings": [
            {"title": "ML Engineer", "externalPath": "/job/Paris/ML-Engineer_JR1",
             "locationsText": "Paris", "postedOn": "Posted Today"},
            {"title": "ML Engineer", "externalPath": "/job/Bangalore/ML-Engineer_JR2",
             "locationsText": "Bangalore Area", "postedOn": "Posted Today"},
        ],
    }
    detail = {"/job/Paris/ML-Engineer_JR1": {
        "jobPostingInfo": {"jobDescription": "<p>Build ML systems</p>", "jobReqId": "JR1"},
    }}
    client = _patch(monkeypatch, [search_page], detail)

    jobs = workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["machine learning"],
                         pages_per_query=1)

    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "workday" and j.external_id == "JR1"
    assert j.title == "ML Engineer" and j.location == "Paris"
    assert j.description == "Build ML systems"
    assert j.url == "https://acme.wd3.myworkdayjobs.com/en-US/AcmeSite/job/Paris/ML-Engineer_JR1"
    # only the France-matched posting triggers a detail fetch
    assert client.gets == ["https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeSite"
                           "/job/Paris/ML-Engineer_JR1"]


def test_fetch_dedups_postings_seen_across_multiple_queries(monkeypatch):
    same_posting = {"title": "AI Engineer", "externalPath": "/job/Paris/AI-Engineer_JR9",
                    "locationsText": "Paris", "postedOn": "Posted Today"}
    page_for_q1 = {"total": 1, "jobPostings": [same_posting]}
    page_for_q2 = {"total": 1, "jobPostings": [same_posting]}
    _patch(monkeypatch, [page_for_q1, page_for_q2])

    jobs = workday.fetch("acme", "wd3", "AcmeSite", "Acme",
                         queries=["machine learning", "artificial intelligence"],
                         pages_per_query=1)

    assert len(jobs) == 1


def test_fetch_stops_paginating_once_total_is_covered(monkeypatch):
    page1 = {"total": 1, "jobPostings": [
        {"title": "AI Engineer", "externalPath": "/job/Paris/AI-Engineer_JR1",
         "locationsText": "Paris", "postedOn": "Posted Today"},
    ]}
    client = _patch(monkeypatch, [page1])   # only one page enqueued -- a 2nd POST would KeyError-safe to {}

    workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["ai"], pages_per_query=5)

    assert len(client.posts) == 1   # stopped after offset >= total, didn't burn through 5 pages


def test_fetch_survives_detail_endpoint_failure(monkeypatch):
    search_page = {"total": 1, "jobPostings": [
        {"title": "ML Engineer", "externalPath": "/job/Paris/ML-Engineer_JR1",
         "locationsText": "Paris", "postedOn": "Posted Today"},
    ]}
    _patch(monkeypatch, [search_page], detail_payloads={})   # no matching detail -> empty stub

    jobs = workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["ml"], pages_per_query=1)

    assert len(jobs) == 1
    assert jobs[0].description == ""   # degrades gracefully, doesn't crash the whole fetch


def test_is_france_shared_with_other_ats_sources():
    assert workday._is_france is ats._is_france


def test_fetch_tracks_pagination_cap_hit_when_total_exceeds_pages_fetched(monkeypatch):
    # 3 pages of 1 posting each, "total": 1000 -- pages_per_query=3 exhausts without
    # ever reaching offset >= total, so real postings are left unfetched.
    pages = [{"total": 1000, "jobPostings": [
        {"title": "AI Engineer", "externalPath": f"/job/Paris/AI-Engineer_JR{i}",
         "locationsText": "Paris", "postedOn": "Posted Today"},
    ]} for i in range(3)]
    _patch(monkeypatch, pages, detail_payloads={})

    with fetch_diag.run_tracking() as t:
        workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["ai"], pages_per_query=3)

    assert t.counts[("workday", "Acme", "pagination_cap_hit")] == 1


def test_fetch_tracks_detail_fetch_failure(monkeypatch):
    class _RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            return Resp({"total": 1, "jobPostings": [
                {"title": "ML Engineer", "externalPath": "/job/Paris/ML-Engineer_JR1",
                 "locationsText": "Paris", "postedOn": "Posted Today"},
            ]})

        def get(self, url):
            raise workday.httpx.HTTPError("boom")

    monkeypatch.setattr(workday.httpx, "Client", lambda *a, **k: _RaisingClient())
    monkeypatch.setattr(workday.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["ml"], pages_per_query=1)

    assert len(jobs) == 1 and jobs[0].description == ""   # job still kept, just degraded
    assert t.counts[("workday", "Acme", "detail_fetch_failed")] == 1


def test_fetch_falls_back_to_bulletfields_when_locationstext_is_empty(monkeypatch):
    # Regression: some Workday tenants (e.g. Renault's site) leave locationsText empty
    # and put the city in bulletFields[0] instead -- silently dropped every match.
    search_page = {"total": 1, "jobPostings": [
        {"title": "Data Scientist", "externalPath": "/job/Lardy/Data-Scientist_JR1",
         "locationsText": "", "bulletFields": ["Lardy", "N - IT & Systems"],
         "postedOn": "Posted Today"},
    ]}
    _patch(monkeypatch, [search_page], detail_payloads={})

    jobs = workday.fetch("acme", "wd3", "AcmeSite", "Acme", queries=["data scientist"],
                         pages_per_query=1)

    assert len(jobs) == 1
    assert jobs[0].location == "Lardy"
