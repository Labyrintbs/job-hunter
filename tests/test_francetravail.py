from jobhunter.sources import francetravail as ft


def test_fetch_returns_empty_without_credentials(monkeypatch):
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
    assert ft.fetch("machine learning engineer") == []


def test_get_token_posts_client_credentials_and_caches(monkeypatch):
    ft._token_cache.clear()
    calls = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "tok-123", "token_type": "Bearer", "expires_in": 1499}

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append((url, data))
        return Resp()

    monkeypatch.setattr(ft.httpx, "post", fake_post)
    token1 = ft._get_token("cid", "csecret")
    token2 = ft._get_token("cid", "csecret")  # should hit the cache, not POST again
    assert token1 == token2 == "tok-123"
    assert len(calls) == 1
    assert calls[0][1]["grant_type"] == "client_credentials"
    assert calls[0][1]["client_id"] == "cid"


def test_to_job_maps_fields():
    offer = {
        "id": "abc123",
        "intitule": "Machine Learning Engineer",
        "description": "Full JD text here.",
        "dateCreation": "2026-09-01T10:00:00.000Z",
        "lieuTravail": {"libelle": "75 - Paris"},
        "entreprise": {"nom": "Acme"},
        "typeContrat": "CDI",
        "origineOffre": {"urlOrigine": "https://example.com/offre/abc123"},
    }
    job = ft._to_job(offer)
    assert job.source == "francetravail"
    assert job.external_id == "abc123"
    assert job.title == "Machine Learning Engineer"
    assert job.company == "Acme"
    assert job.location == "75 - Paris"
    assert job.contract_type == "CDI"
    assert job.url == "https://example.com/offre/abc123"


def test_fetch_paginates_until_short_page(monkeypatch):
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)

    def make_offer(i):
        return {"id": str(i), "intitule": "ML Engineer", "entreprise": {"nom": "Acme"},
                "lieuTravail": {"libelle": "Paris"}, "description": "d"}

    pages = [
        {"resultats": [make_offer(i) for i in range(ft.PAGE_SIZE)]},           # full page -> keep going
        {"resultats": [make_offer(i) for i in range(ft.PAGE_SIZE, ft.PAGE_SIZE + 5)]},  # short page -> stop
    ]

    class Client:
        def __init__(self, *a, **k):
            self._calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            page = pages[self._calls]
            self._calls += 1
            class Resp:
                status_code = 200
                def json(self_inner):
                    return page
            return Resp()

    monkeypatch.setattr(ft.httpx, "Client", Client)
    # A single department (<= MAX_DEPARTEMENTS_PER_REQUEST) -- one batch, so this
    # test exercises pure pagination without the batching loop in play.
    jobs = ft.fetch("ml engineer", departements="75", max_results=1000)
    assert len(jobs) == ft.PAGE_SIZE + 5


def test_fetch_batches_departements_and_dedups_across_batches(monkeypatch):
    # The API rejects more than 5 departements per request (verified live), so a
    # real departements list (e.g. IDF's 8) must be split into batches -- and the
    # same offer can legitimately appear in more than one department's results
    # (multi-site postings), so results must be deduped by id across batches.
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)

    def make_offer(i):
        return {"id": str(i), "intitule": "ML Engineer", "entreprise": {"nom": "Acme"},
                "lieuTravail": {"libelle": "Paris"}, "description": "d"}

    class Client:
        def __init__(self, *a, **k):
            self.requested_departements = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            self.requested_departements.append(params["departement"])
            # batch 1 -> offers 0,1 ; batch 2 -> offers 1,2 (id 1 overlaps -> dedup)
            offers = [make_offer(0), make_offer(1)] if len(self.requested_departements) == 1 \
                else [make_offer(1), make_offer(2)]
            class Resp:
                status_code = 200
                def json(self_inner):
                    return {"resultats": offers}
            return Resp()

    client_holder = {}

    def fake_client(*a, **k):
        c = Client(*a, **k)
        client_holder["client"] = c
        return c

    monkeypatch.setattr(ft.httpx, "Client", fake_client)
    jobs = ft.fetch("ml engineer", departements=ft.IDF_DEPARTEMENTS, max_results=1000)

    client = client_holder["client"]
    assert client.requested_departements == ["75,92,93,94,77", "78,91,95"]
    assert sorted(j.external_id for j in jobs) == ["0", "1", "2"]


class _CountClient:
    """Serves a fixed Content-Range total per request, recording every params dict."""
    def __init__(self, total_by_call):
        self._totals = list(total_by_call)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.calls.append(dict(params or {}))
        total = self._totals[len(self.calls) - 1]
        class Resp:
            status_code = 206
            headers = {"Content-Range": f"offres 0-0/{total}"}
        return Resp()


def test_count_returns_none_without_credentials(monkeypatch):
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
    assert ft.count(domaine="M18") is None


def test_count_parses_content_range_total(monkeypatch):
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")

    client = _CountClient([9679])
    monkeypatch.setattr(ft.httpx, "Client", lambda *a, **k: client)

    total = ft.count(domaine="M18")

    assert total == 9679
    assert client.calls == [{"domaine": "M18"}]   # no departement key -- nationwide


def test_count_passes_arbitrary_filters_through(monkeypatch):
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")

    client = _CountClient([215])
    monkeypatch.setattr(ft.httpx, "Client", lambda *a, **k: client)

    total = ft.count(codeROME="M1889")

    assert total == 215
    assert client.calls == [{"codeROME": "M1889"}]


def test_count_batches_departements_and_sums_totals(monkeypatch):
    # IDF (8 departments) exceeds the 5-department cap -- count() must batch
    # the same way fetch() does, and SUM each batch's total (safe: a posting's
    # departement is a single value, so batches partition without overlap).
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")

    client = _CountClient([120, 45])
    monkeypatch.setattr(ft.httpx, "Client", lambda *a, **k: client)

    total = ft.count(departements=ft.IDF_DEPARTEMENTS, domaine="M18")

    assert total == 165
    assert client.calls == [
        {"domaine": "M18", "departement": "75,92,93,94,77"},
        {"domaine": "M18", "departement": "78,91,95"},
    ]


def test_count_returns_none_on_a_failed_batch(monkeypatch):
    ft._token_cache.clear()
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(ft, "_get_token", lambda cid, secret: "tok")

    class FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            class Resp:
                status_code = 400
                headers = {}
            return Resp()

    monkeypatch.setattr(ft.httpx, "Client", lambda *a, **k: FailingClient())

    assert ft.count(domaine="M18") is None
