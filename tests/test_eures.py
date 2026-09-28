from jobhunter import fetch_diag, match
from jobhunter.sources import eures


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
        self.bodies = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self.bodies.append(json)
        return Resp(self._pages.pop(0) if self._pages else {"numberRecords": 0, "jvs": []})


def _jv(jid="1", title="Data Scientist", employer="Acme", location_map=None, desc="<p>Build ML</p>"):
    return {
        "id": jid, "title": title, "description": desc,
        "creationDate": 1735689600000,  # 2025-01-01 UTC in ms
        "employer": {"name": employer},
        "locationMap": location_map if location_map is not None else {"FR": ["FR101"]},
    }


def test_fetch_parses_fields_and_strips_html(monkeypatch):
    page = {"numberRecords": 1, "jvs": [_jv()]}
    client = Client([page])
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    jobs = eures.fetch("data scientist", max_hits=10)

    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "eures" and j.external_id == "1"
    assert j.company == "Acme" and "Build ML" in j.description and "<p>" not in j.description
    assert j.posted_at == "2025-01-01"
    assert j.url == "https://europa.eu/eures/portal/jv-se/jv-details/1?lang=fr"


def test_paris_nuts_code_maps_to_idf_geo_tier(monkeypatch):
    page = {"numberRecords": 1, "jvs": [_jv(location_map={"FR": ["FR101"]})]}
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    jobs = eures.fetch("data scientist", max_hits=10)

    assert jobs[0].location == "Paris, Île-de-France"
    cfg = {"locations": ["paris", "ile-de-france", "île-de-france"], "major_cities": [], "europe_countries": []}
    assert match.geo_tier(jobs[0].location, cfg) == "idf"


def test_other_idf_department_nuts_code_still_maps_to_idf_geo_tier(monkeypatch):
    # Regression: geo_tier() only recognizes IDF via place names like
    # "ile-de-france", not plain department names -- FR105 (Hauts-de-Seine)
    # must map to the region name, not the department name, or it would
    # silently score as "outside" instead of "idf".
    page = {"numberRecords": 1, "jvs": [_jv(location_map={"FR": ["FR105"]})]}
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    jobs = eures.fetch("data scientist", max_hits=10)

    assert jobs[0].location == "Île-de-France"
    cfg = {"locations": ["paris", "ile-de-france", "île-de-france"], "major_cities": [], "europe_countries": []}
    assert match.geo_tier(jobs[0].location, cfg) == "idf"


def test_non_idf_french_code_falls_back_to_plain_france(monkeypatch):
    page = {"numberRecords": 1, "jvs": [_jv(location_map={"FR": ["FR712"]})]}  # Lyon-area NUTS3, not IDF
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: Client([page]))
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    jobs = eures.fetch("data scientist", max_hits=10)

    assert jobs[0].location == "France"


def test_fetch_tracks_pagination_cap_hit(monkeypatch):
    # 50 results per page (PAGE_SIZE), numberRecords far exceeds max_hits --
    # the loop stops because it filled max_hits, not because it ran out.
    page1 = {"numberRecords": 500, "jvs": [_jv(jid=str(i)) for i in range(50)]}
    page2 = {"numberRecords": 500, "jvs": [_jv(jid=str(i)) for i in range(50, 100)]}
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: Client([page1, page2]))
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    with fetch_diag.run_tracking() as t:
        jobs = eures.fetch("data scientist", max_hits=60)

    assert len(jobs) == 60
    assert t.counts[("eures", "", "pagination_cap_hit")] == 1


def test_fetch_stops_when_a_page_is_empty(monkeypatch):
    page = {"numberRecords": 0, "jvs": []}
    client = Client([page])
    monkeypatch.setattr(eures.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(eures.time, "sleep", lambda *_: None)

    jobs = eures.fetch("nonexistent role", max_hits=50)

    assert jobs == []
    assert len(client.bodies) == 1
