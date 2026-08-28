from jobhunter import enrich


class Resp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, Resp(404, ""))


def test_strip_html():
    assert enrich._strip_html("<p>Hello&amp; <b>world</b></p>") == "Hello& world"
    assert enrich._strip_html("<script>x=1</script><p>keep</p>") == "keep"


def test_linkedin_enrichment_hits_detail_endpoint_and_extracts_markup():
    jid = "4123456789"
    url = enrich._LI_DETAIL_URL.format(id=jid)
    body = '<div class="show-more-less-html__markup">We use <b>PyTorch</b>; 3 yrs.</div>'
    c = Client({url: Resp(200, body)})
    text = enrich.fetch_full_text("linkedin", jid, "http://ignored", client=c)
    assert "PyTorch" in text and "3 yrs" in text
    assert c.calls == [url]                       # used the detail endpoint, not the card url


def test_generic_enrichment_strips_page():
    c = Client({"http://job": Resp(200, "<html><body><h1>Role</h1><p>Great ML job here</p></body></html>")})
    text = enrich.fetch_full_text("wttj", "abc", "http://job", client=c)
    assert "Great ML job here" in text


def test_enrichment_none_on_failure():
    c = Client({})                                # everything 404s
    assert enrich.fetch_full_text("wttj", "abc", "http://job", client=c) is None
    assert enrich.fetch_full_text("linkedin", "notdigits", "", client=c) is None
