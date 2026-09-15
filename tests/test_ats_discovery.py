from jobhunter.sources import ats_discovery


class Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class Client:
    def __init__(self, pages):
        self.pages = pages   # {url: Resp}
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, Resp(404))


def test_slugs_generates_hyphenated_concatenated_and_first_word_variants():
    variants = ats_discovery._slugs("Acme Corp")
    assert variants == ["acme-corp", "acmecorp", "acme"]


def test_slugs_strips_accents_and_punctuation():
    variants = ats_discovery._slugs("Crédit Agricole, S.A.")
    assert "credit-agricole-s-a" in variants
    assert "credit" in variants


def test_slugs_empty_for_blank_name():
    assert ats_discovery._slugs("   ") == []


def test_probe_finds_greenhouse_board_with_france_posting():
    url = "https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs?content=true"
    client = Client({url: Resp(200, {"jobs": [{"location": {"name": "Paris, France"}}]})})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None
    assert "greenhouse" in hit and "acme-corp" in hit


def test_probe_skips_board_with_no_france_postings():
    url = "https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs?content=true"
    client = Client({url: Resp(200, {"jobs": [{"location": {"name": "Berlin, Germany"}}]})})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is None


def test_probe_tries_lever_raw_list_response_shape():
    url = "https://api.lever.co/v0/postings/acme-corp?mode=json"
    client = Client({url: Resp(200, [{"categories": {"location": "Lyon, France"}}])})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and "lever" in hit


def test_probe_returns_none_when_nothing_matches():
    client = Client({})   # every url 404s

    hit = ats_discovery.probe("Totally Unknown Startup", client=client)

    assert hit is None


def test_probe_tries_multiple_slug_variants_before_giving_up():
    # only the concatenated variant ("acmecorp") has a real board -- the hyphenated
    # one tried first must 404 and fall through, not stop the search early.
    url = "https://boards-api.greenhouse.io/v1/boards/acmecorp/jobs?content=true"
    client = Client({url: Resp(200, {"jobs": [{"location": {"name": "Paris, France"}}]})})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and "acmecorp" in hit
