import json

from jobhunter.sources import wttj


def _hit(remote="no", city="Paris", state="Ile-de-France", country="France"):
    return {
        "objectID": "abc123",
        "name": "ML Engineer",
        "organization": {"name": "Acme", "slug": "acme"},
        "slug": "ml-engineer",
        "offices": [{"city": city, "state": state, "country": country}],
        "remote": remote,
    }


def test_to_job_tags_fulltime_remote_hit():
    job = wttj._to_job(_hit(remote="fulltime", city="Madrid", state="Madrid", country="Spain"))
    assert job.location == "Madrid, Madrid, Spain - Remote"


def test_to_job_leaves_non_remote_hit_unchanged():
    job = wttj._to_job(_hit(remote="no", city="Madrid", state="Madrid", country="Spain"))
    assert job.location == "Madrid, Madrid, Spain"


def test_to_job_does_not_double_tag_already_remote_location():
    job = wttj._to_job(_hit(remote="fulltime", city="Remote", state="", country="Spain"))
    assert job.location.lower().count("remote") == 1


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Client:
    def __init__(self, response_body):
        self._response_body = response_body
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, params=None, headers=None, content=None):
        self.posts.append(json.loads(content))
        return _Resp(self._response_body)


def test_fetch_remote_only_builds_two_group_facet_filter(monkeypatch):
    client = _Client({"hits": [], "nbPages": 1})
    monkeypatch.setattr(wttj.httpx, "Client", lambda *a, **k: client)
    wttj.fetch(query="ai engineer", remote_only=True, extra_countries=["Germany", "Spain"])
    assert client.posts[0]["facetFilters"] == [
        ["remote:fulltime"],
        ["offices.country:Germany", "offices.country:Spain"],
    ]


def test_fetch_default_country_facet_unchanged(monkeypatch):
    client = _Client({"hits": [], "nbPages": 1})
    monkeypatch.setattr(wttj.httpx, "Client", lambda *a, **k: client)
    wttj.fetch(query="ai engineer", country="France")
    assert client.posts[0]["facetFilters"] == [["offices.country:France"]]
