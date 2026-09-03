from jobhunter.sources import linkedin

CARD = """
<li>
  <div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:4455825967">
    <a class="base-card__full-link" href="https://fr.linkedin.com/jobs/view/ml-eng-at-acme-4455825967?position=1&amp;refId=x">
      <span class="sr-only"> AI &amp; Machine Learning Engineer (M/F) </span>
    </a>
    <h3 class="base-search-card__title"> AI &amp; Machine Learning Engineer (M/F) </h3>
    <h4 class="base-search-card__subtitle"><a class="hidden-nested-link">TNP Consultants</a></h4>
    <span class="job-search-card__location">Levallois-Perret, Île-de-France, France</span>
    <time class="job-search-card__listdate" datetime="2026-08-24">1 week ago</time>
  </div>
</li>
"""


def test_parse_card_fields_and_entity_decode():
    jobs = [j for j in (linkedin._parse_card(c) for c in linkedin._CARD_RE.findall(CARD)) if j]
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "linkedin"
    assert j.external_id == "4455825967"
    assert j.title == "AI & Machine Learning Engineer (M/F)"     # &amp; decoded
    assert j.company == "TNP Consultants"
    assert j.location == "Levallois-Perret, Île-de-France, France"
    assert j.url == "https://fr.linkedin.com/jobs/view/ml-eng-at-acme-4455825967"  # query stripped
    assert j.posted_at == "2026-08-24"


def test_card_without_urn_is_skipped():
    assert linkedin._parse_card("<div>no urn here</div>") is None


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _Client:
    """Serves canned responses in order, recording every URL requested."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return self._responses.pop(0) if self._responses else _Resp(200, "")


def test_fetch_covers_every_query_location_pair(monkeypatch):
    monkeypatch.setattr(linkedin.time, "sleep", lambda *_: None)
    seen_clients = []

    def fake_client(*a, **k):
        c = _Client([])  # every request -> empty 200, so each pair stops after page 0
        seen_clients.append(c)
        return c

    class _CM:
        def __enter__(self):
            return fake_client()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(linkedin.httpx, "Client", lambda *a, **k: _CM())
    linkedin.fetch(["ml engineer", "nlp engineer"], ["Paris", "Lyon"], max_pages=1)
    # one client context reused across pairs, and every (query, location) pair issued a request
    assert len(seen_clients) == 1
    assert len(seen_clients[0].urls) == 4  # 2 queries x 2 locations


def test_fetch_retries_429_with_backoff_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(linkedin.time, "sleep", lambda s: sleeps.append(s))
    client = _Client([_Resp(429), _Resp(429), _Resp(200, CARD)])

    class _CM:
        def __enter__(self):
            return client

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(linkedin.httpx, "Client", lambda *a, **k: _CM())
    jobs = linkedin.fetch(["ml"], ["Paris"], max_pages=1, max_retries=3, backoff_base=0.01)
    assert len(jobs) == 1 and jobs[0].external_id == "4455825967"
    assert len(client.urls) == 3   # two 429s retried, third attempt served the page


def test_fetch_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(linkedin.time, "sleep", lambda *_: None)
    client = _Client([_Resp(429), _Resp(429), _Resp(429)])

    class _CM:
        def __enter__(self):
            return client

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(linkedin.httpx, "Client", lambda *a, **k: _CM())
    jobs = linkedin.fetch(["ml"], ["Paris"], max_pages=1, max_retries=2, backoff_base=0.01)
    assert jobs == []
    assert len(client.urls) == 3   # initial + 2 retries, then gave up
