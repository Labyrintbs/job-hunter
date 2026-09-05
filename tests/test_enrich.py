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


def test_looks_like_scraped_chrome_flags_widget_markup_leakage():
    # A real fragment pulled from a corrupted HelloWork fetch (job 316): mostly
    # Stimulus/Turbo controller attributes and analytics hooks, not job text.
    garbage = (
        'tracker#identify turbo:frame-missing@window->content-missing#handleFrameMissing" '
        'auto-submittable#submit" > toggle#add from-account-data:copied:Initials@document'
        '->toggle#remove" data-toggle-target="toToggle"> from-account-data#copy" '
        'data-from-account-data-copy-value="Initials"> analytics#push" > Mon espace '
        'analytics#push" > Mes CV vus input-checker#uncheck"> input-checker#uncheck">'
    )
    assert enrich._looks_like_scraped_chrome(garbage * 2)


def test_looks_like_scraped_chrome_does_not_flag_real_job_text():
    real = ("We are looking for a Machine Learning Engineer with 3+ years of experience "
            "in Python, PyTorch, and production deployment on AWS. You will design and "
            "maintain ML pipelines, collaborate with the data team, and own model "
            "monitoring end to end.") * 5
    assert not enrich._looks_like_scraped_chrome(real)


def test_generic_enrichment_returns_none_when_page_is_mostly_scraped_chrome():
    garbage_html = ("<div data-controller=\"atc\" data-action=\"click->toggle#add\">x</div>"
                     "<div data-controller=\"y\" data-action=\"click->toggle#remove\">x</div>"
                     "<span data-controller=\"z\">analytics#push</span>"
                     "<span>input-checker#uncheck</span><span>toggle#expand</span>"
                     "<span>toggle#collapse</span><span>toggle#add</span>"
                     "<span>analytics#push</span><span>data-action=\"foo\"</span>"
                     "<p>Ingénieur IA H/F, un vrai poste avec un peu de texte réel</p>")
    c = Client({"http://job": Resp(200, garbage_html)})
    assert enrich.fetch_full_text("hellowork", "abc", "http://job", client=c) is None
