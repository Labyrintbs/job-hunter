from jobhunter.sources import ats


def test_is_france_recognizes_city_only_locations():
    # Regression: large-corporate ATS boards (esp. Workday) often report a bare city
    # name with no "France" suffix -- these were silently dropped before the fix.
    assert ats._is_france("Guyancourt") is True
    assert ats._is_france("Lardy") is True
    assert ats._is_france("Toulouse") is True
    assert ats._is_france("Bangalore Area") is False


class Resp:
    def __init__(self, payload, status_code=200, text=""):
        self._p = payload
        self.status_code = status_code
        self.text = text

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
    ], workday_queries=["machine learning", "computer vision"])
    assert calls == [("alliancewd", "wd3", "renault-group-careers", "Renault",
                       {"locale": "fr-FR", "queries": ["machine learning", "computer vision"]})]


def test_fetch_all_tolerates_malformed_workday_entry(monkeypatch):
    monkeypatch.setattr(ats.time, "sleep", lambda *_: None)
    out = ats.fetch_all([{"name": "Missing Fields", "ats": "workday"}])   # no tenant/wd_host/site
    assert out == []


def test_teamtailor_france_filter_and_multi_location_join(monkeypatch):
    _patch(monkeypatch, {"items": [
        {"id": "t1", "title": "Analytics PM", "url": "http://t/1",
         "_jobposting": {"description": "<p>d</p>", "datePosted": "2026-09-01T00:00:00+02:00",
                          "jobLocation": [{"address": {"addressLocality": "Paris", "addressCountry": "FR"}}]}},
        {"id": "t2", "title": "Multi-location freelance", "url": "http://t/2",
         "_jobposting": {"jobLocation": [
             {"address": {"addressLocality": "Berlin", "addressCountry": "DE"}},
             {"address": {"addressRegion": "USA", "addressCountry": "US"}},
         ]}},
    ]})
    jobs = ats.fetch_teamtailor("dolead", "Dolead")
    assert len(jobs) == 1
    assert jobs[0].source == "teamtailor" and jobs[0].external_id == "t1"
    assert jobs[0].location == "Paris, FR"


def test_teamtailor_warns_on_suspicious_100_item_count(monkeypatch, capsys):
    # A low-confidence report claims a 100-item server-side cap with no cursor --
    # couldn't confirm or rule this out live, so an exact-100 response is flagged
    # for a human to check rather than silently trusted.
    items = [{"id": f"t{i}", "title": "X", "url": "http://t", "_jobposting": {}} for i in range(100)]
    _patch(monkeypatch, {"items": items})
    ats.fetch_teamtailor("acme", "Acme", country_only=False)
    assert "possible undocumented cap" in capsys.readouterr().out


class _XmlClient:
    def __init__(self, text, status_code=200):
        self._text = text
        self._status = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return Resp(None, status_code=self._status, text=self._text)


_PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position>
    <id>1</id>
    <office>Paris, France</office>
    <name>ML Engineer</name>
    <jobDescriptions>
        <jobDescription><name>Mission</name><value><![CDATA[<p>Build stuff</p>]]></value></jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <createdAt>2026-09-01T00:00:00+00:00</createdAt>
</position>
<position>
    <id>2</id>
    <office>Berlin, Germany</office>
    <name>Other</name>
</position>
</workzag-jobs>"""


def test_personio_xml_parse_and_france_filter(monkeypatch):
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: _XmlClient(_PERSONIO_XML))
    jobs = ats.fetch_personio("acme", "Acme")
    assert len(jobs) == 1
    assert jobs[0].source == "personio" and jobs[0].external_id == "1"
    assert jobs[0].url == "https://acme.jobs.personio.com/job/1"
    assert "Build stuff" in jobs[0].description


def test_personio_404_means_feed_not_enabled(monkeypatch):
    # The XML feed is opt-in per Personio customer -- a 404 is the normal "not
    # turned on" state, confirmed live, not a fetch error.
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: _XmlClient("", status_code=404))
    jobs = ats.fetch_personio("acme", "Acme")
    assert jobs == []


_SUCCESSFACTORS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel><title>Acme</title>
<item>
<title>ML Engineer (Paris)</title>
<description><![CDATA[<p>Build stuff</p>]]></description>
<link>https://job.acme.com/job/1</link>
<guid>1</guid>
<g:id>1</g:id>
<g:employer>Acme Corp</g:employer>
<g:location>Paris, France, FR</g:location>
<pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate>
</item>
<item>
<title>Other Role</title>
<description><![CDATA[<p>d</p>]]></description>
<link>https://job.acme.com/job/2</link>
<guid>2</guid>
<g:id>2</g:id>
<g:employer>Acme Corp</g:employer>
<g:location>Berlin, Germany, DE</g:location>
</item>
</channel></rss>"""


def test_successfactors_rss_namespace_parse_and_france_filter(monkeypatch):
    monkeypatch.setattr(ats.httpx, "Client", lambda *a, **k: _XmlClient(_SUCCESSFACTORS_XML))
    jobs = ats.fetch_successfactors("job.acme.com", "Acme")
    assert len(jobs) == 1
    assert jobs[0].source == "successfactors" and jobs[0].external_id == "1"
    assert jobs[0].company == "Acme Corp"
    assert jobs[0].location == "Paris, France, FR"
    assert jobs[0].posted_at   # parsed from pubDate
    assert "Build stuff" in jobs[0].description
