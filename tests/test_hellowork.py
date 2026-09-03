from jobhunter.sources import hellowork

# Trimmed from a real HelloWork search-results page, two cards.
PAGE = """
<li><div data-cy="serpCard">
  <a href="/fr-fr/emplois/82575677.html" data-cy="offerTitle" title="Machine Learning Engineer H/F - Team.is">
    <h3 class="inline">
      <p class="typo-l sm:small-group:typo-l sm:typo-xl">Machine Learning Engineer H/F</p>
      <p class="typo-s inline">Team.is</p>
    </h3>
  </a>
  <div data-cy="localisationCard">
    Paris 2e - 75
  </div>
  <div data-cy="contractCard">
    CDI
  </div>
</div></li>
<li><div data-cy="serpCard">
  <a href="/fr-fr/emplois/81401230.html" data-cy="offerTitle" title="Alternant Machine Learning Engineer H/F">
    <h3 class="inline">
      <p class="typo-l sm:small-group:typo-l sm:typo-xl">Alternant Machine Learning Engineer H/F</p>
      <p class="typo-s inline">NEXA Digital School</p>
    </h3>
  </a>
  <div data-cy="localisationCard">
    Île-de-France
  </div>
  <div data-cy="contractCard">
    Alternance
  </div>
</div></li>
"""


def test_parse_card_extracts_all_fields():
    cards = PAGE.split(hellowork._CARD_SPLIT)[1:]
    assert len(cards) == 2
    jobs = [j for j in (hellowork._parse_card(c) for c in cards) if j]
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "hellowork"
    assert j.external_id == "82575677"
    assert j.title == "Machine Learning Engineer H/F"
    assert j.company == "Team.is"
    assert j.location == "Paris 2e - 75"
    assert j.contract_type == "CDI"
    assert j.url == "https://www.hellowork.com/fr-fr/emplois/82575677.html"


def test_card_without_id_is_skipped():
    assert hellowork._parse_card("<div>no offer link here</div>") is None


def test_fetch_single_request_no_pagination(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        class Resp:
            status_code = 200
            text = PAGE
        return Resp()

    monkeypatch.setattr(hellowork.httpx, "get", fake_get)
    jobs = hellowork.fetch("machine learning engineer", "Paris")
    assert len(jobs) == 2
    assert len(calls) == 1   # exactly one request -- no pagination attempted
    assert "k=machine" in calls[0] and "l=Paris" in calls[0]


def test_fetch_non_200_returns_empty(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        class Resp:
            status_code = 403
            text = ""
        return Resp()

    monkeypatch.setattr(hellowork.httpx, "get", fake_get)
    assert hellowork.fetch("x", "Paris") == []
