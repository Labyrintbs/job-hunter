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
        {"resultats": [make_offer(i) for i in range(ft.PAGE_SIZE)]},   # full page -> keep going
        {"resultats": [make_offer(i) for i in range(5)]},              # short page -> stop
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
    jobs = ft.fetch("ml engineer", max_results=1000)
    assert len(jobs) == ft.PAGE_SIZE + 5
