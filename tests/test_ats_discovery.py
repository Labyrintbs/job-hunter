import httpx
import pytest

from jobhunter import fetch_diag
from jobhunter.sources import ats_discovery


class Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

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

    assert hit == ats_discovery.Hit("greenhouse", "acme-corp", 1)


def test_probe_skips_board_with_no_france_postings():
    url = "https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs?content=true"
    client = Client({url: Resp(200, {"jobs": [{"location": {"name": "Berlin, Germany"}}]})})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is None


def test_probe_tries_lever_raw_list_response_shape():
    url = "https://api.lever.co/v0/postings/acme-corp?mode=json"
    client = Client({url: Resp(200, [{"categories": {"location": "Lyon, France"}}])})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and hit.ats == "lever"


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

    assert hit is not None and hit.token == "acmecorp"


def test_probe_finds_teamtailor_board_with_france_posting():
    url = "https://acme-corp.teamtailor.com/jobs.json"
    client = Client({url: Resp(200, {"items": [
        {"_jobposting": {"jobLocation": [{"address": {"addressLocality": "Paris", "addressCountry": "FR"}}]}},
    ]})})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and hit.ats == "teamtailor"


def test_probe_finds_personio_xml_board_with_france_posting():
    url = "https://acme-corp.jobs.personio.com/xml?language=en"
    xml = "<workzag-jobs><position><office>Paris, France</office></position></workzag-jobs>"
    client = Client({url: Resp(200, text=xml)})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and hit.ats == "personio"


def test_probe_skips_personio_board_with_malformed_xml():
    url = "https://acme-corp.jobs.personio.com/xml?language=en"
    client = Client({url: Resp(200, text="not valid xml <<<")})

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is None


class RaisingClient:
    def get(self, url):
        raise httpx.ConnectError("boom")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_probe_raises_incomplete_when_a_board_check_fails_transiently(status):
    # A rate-limited/erroring board isn't evidence of "no board" -- the caller must
    # retry later rather than record a permanent miss.
    url = "https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs?content=true"
    client = Client({url: Resp(status)})   # everything else 404s

    with pytest.raises(ats_discovery.ProbeIncomplete):
        ats_discovery.probe("Acme Corp", client=client)


def test_probe_raises_incomplete_on_network_errors_and_tracks_them():
    with fetch_diag.run_tracking() as t:
        with pytest.raises(ats_discovery.ProbeIncomplete):
            ats_discovery.probe("Acme Corp", client=RaisingClient())
    assert t.counts[("ats_discovery", "Acme Corp", "probe_error")] > 0


def test_probe_plain_404s_are_a_definite_miss_not_incomplete():
    assert ats_discovery.probe("Acme Corp", client=Client({})) is None


def test_probe_returns_a_hit_even_if_an_earlier_check_failed_transiently():
    url = "https://api.lever.co/v0/postings/acme-corp?mode=json"
    client = Client({
        "https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs?content=true": Resp(503),
        url: Resp(200, [{"categories": {"location": "Lyon, France"}}]),
    })

    hit = ats_discovery.probe("Acme Corp", client=client)

    assert hit is not None and hit.ats == "lever"
