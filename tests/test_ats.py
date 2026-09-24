from jobhunter.sources import ats


def test_is_france_recognizes_city_only_locations():
    # Regression: large-corporate ATS boards (esp. Workday) often report a bare city
    # name with no "France" suffix -- these were silently dropped before the fix.
    assert ats._is_france("Guyancourt") is True
    assert ats._is_france("Lardy") is True
    assert ats._is_france("Toulouse") is True
    assert ats._is_france("Bangalore Area") is False


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


class _SmartRecruitersClient:
    """Serves canned page bodies in order, recording every URL requested."""
    def __init__(self, pages):
        self._pages = list(pages)
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self.urls.append(url)
        return Resp(self._pages.pop(0))


def test_smartrecruiters_paginates_past_one_page(monkeypatch):
    # Regression: Veolia (2,941 open postings) and Sopra Steria (2,014) both
    # exceeded the old hardcoded limit=100/no-pagination fetch, silently
    # dropping >95% of their postings -- confirmed live.
    page1 = {"content": [{"id": "s1", "name": "A", "location": {"country": "fr"}}], "totalFound": 150}
    page2 = {"content": [{"id": "s2", "name": "B", "location": {"country": "fr"}}], "totalFound": 150}
    client = _SmartRecruitersClient([page1, page2])
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: client)

    jobs = ats.fetch_smartrecruiters("acme", "Acme")

    assert [j.external_id for j in jobs] == ["s1", "s2"]
    assert len(client.urls) == 2
    assert "offset=0" in client.urls[0] and "offset=100" in client.urls[1]


def test_smartrecruiters_stops_once_totalfound_covered(monkeypatch):
    page = {"content": [{"id": "s1", "name": "A", "location": {"country": "fr"}}], "totalFound": 1}
    client = _SmartRecruitersClient([page])
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: client)

    jobs = ats.fetch_smartrecruiters("acme", "Acme")

    assert len(jobs) == 1 and len(client.urls) == 1


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
        {"name": "Bad", "ats": "not-a-real-ats", "token": "bad"},   # unknown -> skipped, no crash
    ])
    assert len(out) == 1 and out[0].source == "ashby"
    assert "greenhouse" in ats.SUPPORTED_ATS and "workable" in ats.SUPPORTED_ATS


def test_fetch_all_dispatches_workday_with_its_own_shape(monkeypatch):
    # Workday has no single token -- fetch_all routes it to workday.fetch with the
    # (tenant, wd_host, site) triple instead, bypassing the token-based FETCHERS map.
    from jobhunter.sources import workday
    calls = []
    monkeypatch.setattr(workday, "fetch", lambda tenant, wd_host, site, name, **kw:
                        calls.append((tenant, wd_host, site, name, kw)) or [])
    monkeypatch.setattr(ats.time, "sleep", lambda *_: None)
    ats.fetch_all([
        {"name": "Renault", "ats": "workday", "tenant": "alliancewd", "wd_host": "wd3",
         "site": "renault-group-careers", "locale": "fr-FR"},
    ])
    assert calls == [("alliancewd", "wd3", "renault-group-careers", "Renault", {"locale": "fr-FR"})]


def test_fetch_all_tolerates_malformed_workday_entry(monkeypatch):
    monkeypatch.setattr(ats.time, "sleep", lambda *_: None)
    out = ats.fetch_all([{"name": "Missing Fields", "ats": "workday"}])   # no tenant/wd_host/site
    assert out == []
