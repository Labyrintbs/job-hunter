from jobhunter.sources import arbeitnow


def _item(slug="acme-ml-1", remote=False, location="Berlin", description="<p>Build <b>ML</b> models.</p>",
          job_types=None, created_at=1700000000):
    return {
        "slug": slug,
        "company_name": "Acme",
        "title": "Machine Learning Engineer",
        "location": location,
        "remote": remote,
        "url": f"https://www.arbeitnow.com/jobs/companies/acme/{slug}",
        "description": description,
        "job_types": job_types or ["Full-time"],
        "created_at": created_at,
    }


def test_to_job_tags_remote_field():
    job = arbeitnow._to_job(_item(remote=True, location="Konstanz"))
    assert job.location == "Konstanz - Remote"


def test_to_job_leaves_non_remote_unchanged():
    job = arbeitnow._to_job(_item(remote=False, location="Berlin"))
    assert job.location == "Berlin"


def test_to_job_does_not_double_tag_already_remote_location():
    job = arbeitnow._to_job(_item(remote=True, location="Remote job"))
    assert job.location.lower().count("remote") == 1


def test_to_job_strips_html_from_description():
    job = arbeitnow._to_job(_item(description="<p>Build <b>ML</b> models &amp; ship.</p>"))
    assert "<" not in job.description
    assert "ML" in job.description
    assert "&amp;" not in job.description and "&" in job.description   # entity decoded


def test_to_job_maps_core_fields():
    job = arbeitnow._to_job(_item())
    assert job.source == "arbeitnow"
    assert job.external_id == "acme-ml-1"
    assert job.company == "Acme"
    assert job.title == "Machine Learning Engineer"
    assert job.contract_type == "Full-time"
    assert job.posted_at == "2023-11-14"   # 1700000000 UTC


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Client:
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
        return _Resp(self._pages.pop(0) if self._pages else {"data": [], "links": {}})


def test_fetch_paginates_via_links_next(monkeypatch):
    page1 = {"data": [_item(slug="a")], "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}}
    page2 = {"data": [_item(slug="b")], "links": {"next": None}}
    client = _Client([page1, page2])
    monkeypatch.setattr(arbeitnow.httpx, "Client", lambda *a, **k: client)

    jobs = arbeitnow.fetch(max_pages=5)

    assert [j.external_id for j in jobs] == ["a", "b"]
    assert len(client.urls) == 2   # stopped once links.next was None, not exhausted max_pages


def test_fetch_stops_at_max_pages_even_with_more_next_links(monkeypatch):
    page = {"data": [_item(slug="a")], "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}}
    client = _Client([page, page, page, page, page])
    monkeypatch.setattr(arbeitnow.httpx, "Client", lambda *a, **k: client)

    jobs = arbeitnow.fetch(max_pages=2)

    assert len(client.urls) == 2
    assert len(jobs) == 2


def test_fetch_stops_when_a_page_returns_no_data(monkeypatch):
    client = _Client([{"data": [], "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}}])
    monkeypatch.setattr(arbeitnow.httpx, "Client", lambda *a, **k: client)

    jobs = arbeitnow.fetch(max_pages=5)

    assert jobs == []
    assert len(client.urls) == 1


def test_fetch_skips_items_without_a_slug(monkeypatch):
    page = {"data": [_item(slug="a"), {**_item(), "slug": ""}], "links": {"next": None}}
    client = _Client([page])
    monkeypatch.setattr(arbeitnow.httpx, "Client", lambda *a, **k: client)

    jobs = arbeitnow.fetch(max_pages=5)

    assert [j.external_id for j in jobs] == ["a"]
