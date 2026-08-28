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
