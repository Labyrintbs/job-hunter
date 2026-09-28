from jobhunter import fetch_diag
from jobhunter.sources import aijobs


class Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class Client:
    def __init__(self, pages):
        self._pages = list(pages)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return Resp(self._pages.pop(0) if self._pages else {"matched": 0, "jobs": []})


def _job(title="ML Engineer", company="Mistral AI", location="Paris", remote=False, apply_url=None):
    d = {"title": title, "company": company, "location": location, "remote": remote,
         "posted": "2026-09-25", "url": "https://artificialintelligencejobs.co/jobs/x"}
    if apply_url:
        d["apply_url"] = apply_url
    return d


def test_fetch_keeps_france_job_and_prefers_apply_url(monkeypatch):
    page = {"matched": 1, "jobs": [_job(apply_url="https://jobs.ashbyhq.com/mistral.ai/abc")]}
    monkeypatch.setattr(aijobs.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(aijobs.time, "sleep", lambda *_: None)

    jobs = aijobs.fetch("machine learning", max_hits=10)

    assert len(jobs) == 1
    assert jobs[0].url == "https://jobs.ashbyhq.com/mistral.ai/abc"
    assert jobs[0].company == "Mistral AI"


def test_fetch_falls_back_to_own_url_without_apply_url(monkeypatch):
    page = {"matched": 1, "jobs": [_job()]}
    monkeypatch.setattr(aijobs.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(aijobs.time, "sleep", lambda *_: None)

    jobs = aijobs.fetch("machine learning", max_hits=10)

    assert jobs[0].url == "https://artificialintelligencejobs.co/jobs/x"


def test_fetch_tags_remote_and_tracks_non_france_drop(monkeypatch):
    page = {"matched": 2, "jobs": [
        _job(location="Dublin, Ireland"),
        _job(location="", remote=True, company="RemoteCo"),
    ]}
    monkeypatch.setattr(aijobs.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(aijobs.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = aijobs.fetch("machine learning", max_hits=10)

    assert jobs == []   # neither is France: Dublin, and bare "Remote" has no France signal
    # fetch_diag's counts key is (source, company, reason) -- location is a sample detail
    assert t.counts[("aijobs", "Mistral AI", "non_france")] == 1   # default company from _job()
    assert t.samples[("aijobs", "Mistral AI", "non_france")] == ["Dublin, Ireland"]
    assert t.counts[("aijobs", "RemoteCo", "non_france")] == 1
    assert t.samples[("aijobs", "RemoteCo", "non_france")] == ["Remote"]


def test_fetch_keeps_paris_job(monkeypatch):
    page = {"matched": 1, "jobs": [_job(location="Paris")]}
    monkeypatch.setattr(aijobs.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(aijobs.time, "sleep", lambda *_: None)

    jobs = aijobs.fetch("machine learning", max_hits=10)

    assert len(jobs) == 1 and jobs[0].location == "Paris"


def test_fetch_tracks_pagination_cap_hit(monkeypatch):
    page1 = {"matched": 500, "jobs": [_job(location="Paris") for _ in range(200)]}
    page2 = {"matched": 500, "jobs": [_job(location="Paris") for _ in range(200)]}
    monkeypatch.setattr(aijobs.httpx, "Client", lambda *a, **k: Client([page1, page2]))
    monkeypatch.setattr(aijobs.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = aijobs.fetch("machine learning", max_hits=250)

    assert len(jobs) == 250
    assert t.counts[("aijobs", "", "pagination_cap_hit")] == 1
