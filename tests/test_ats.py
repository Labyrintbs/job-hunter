from jobhunter.sources import ats


class Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class Client:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self.url = url
        return Resp(self._p)


def _patch(monkeypatch, payload):
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: Client(payload))


def test_ashby_france_filter_and_fields(monkeypatch):
    _patch(monkeypatch, {"jobs": [
        {"id": "a1", "title": "ML Engineer", "location": "Paris, France",
         "jobUrl": "http://a/1", "descriptionHtml": "<p>ML role</p>",
         "employmentType": "FullTime", "address": {"postalAddress": {"addressCountry": "France"}}},
        {"id": "a2", "title": "Other", "location": "New York, USA",
         "address": {"postalAddress": {"addressCountry": "USA"}}},
    ]})
    jobs = ats.fetch_ashby("acme", "Acme")
    assert len(jobs) == 1
    assert jobs[0].source == "ashby" and jobs[0].title == "ML Engineer"
    assert "ML role" in jobs[0].description and jobs[0].url == "http://a/1"


def test_smartrecruiters_france_filter(monkeypatch):
    _patch(monkeypatch, {"content": [
        {"id": "s1", "name": "ML Engineer", "location": {"city": "Paris", "region": "IDF", "country": "fr"}},
        {"id": "s2", "name": "Other", "location": {"city": "Berlin", "country": "de"}},
    ]})
    jobs = ats.fetch_smartrecruiters("acme", "Acme")
    assert len(jobs) == 1 and jobs[0].source == "smartrecruiters"
    assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/s1"


def test_recruitee_france_filter(monkeypatch):
    _patch(monkeypatch, {"offers": [
        {"id": 1, "title": "ML Engineer", "city": "Paris", "country": "France",
         "country_code": "fr", "careers_url": "http://r/1", "description": "<p>d</p>"},
        {"id": 2, "title": "Other", "city": "London", "country": "UK", "country_code": "gb"},
    ]})
    jobs = ats.fetch_recruitee("acme", "Acme")
    assert len(jobs) == 1 and jobs[0].source == "recruitee" and jobs[0].url == "http://r/1"


def test_workable_france_filter(monkeypatch):
    _patch(monkeypatch, {"jobs": [
        {"shortcode": "w1", "title": "ML Engineer", "city": "Paris", "country": "France",
         "url": "http://w/1", "description": "<p>d</p>"},
        {"shortcode": "w2", "title": "Other", "city": "Madrid", "country": "Spain"},
    ]})
    jobs = ats.fetch_workable("acme", "Acme")
    assert len(jobs) == 1 and jobs[0].source == "workable" and jobs[0].external_id == "w1"


def test_fetch_all_dispatches_and_tolerates_unknown(monkeypatch):
    monkeypatch.setattr(ats.time, "sleep", lambda *_: None)
    _patch(monkeypatch, {"jobs": [
        {"id": "a1", "title": "ML Engineer", "location": "Paris, France",
         "address": {"postalAddress": {"addressCountry": "France"}}},
    ]})
    out = ats.fetch_all([
        {"name": "Acme", "ats": "ashby", "token": "acme"},
        {"name": "Bad", "ats": "workday", "token": "bad"},   # unknown -> skipped, no crash
    ])
    assert len(out) == 1 and out[0].source == "ashby"
    assert "greenhouse" in ats.SUPPORTED_ATS and "workable" in ats.SUPPORTED_ATS
