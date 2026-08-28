from jobhunter.match import evaluate, is_relevant
from jobhunter.models import Job


def J(title="Machine Learning Engineer", desc="", loc="Paris, Ile-de-France, France",
      contract="FULL_TIME", company="Acme", lang="fr"):
    return Job(source="wttj", external_id="1", title=title, company=company,
               location=loc, description=desc, contract_type=contract, language=lang)


def test_internship_excluded(config):
    score, reasons, keep = evaluate(J(title="Stage - Machine Learning"), config)
    assert keep is False and "excluded" in reasons


def test_alternance_excluded(config):
    _, _, keep = evaluate(J(title="Alternance Data Scientist"), config)
    assert keep is False


def test_non_ml_title_dropped_even_with_ml_boilerplate(config):
    # ATS boilerplate mentions ML in the description but the role is not ML.
    job = J(title="Product Manager", desc="We are a machine learning company using pytorch.")
    assert is_relevant(job, config) is False
    _, _, keep = evaluate(job, config)
    assert keep is False


def test_ml_title_relevant(config):
    assert is_relevant(J(title="Senior Machine Learning Engineer"), config) is True
    assert is_relevant(J(title="Data Scientist"), config) is True
    assert is_relevant(J(title="AI Engineer"), config) is True


def test_paris_ranks_above_other_france(config):
    paris = evaluate(J(loc="Paris, Ile-de-France, France"), config)[0]
    other = evaluate(J(loc="Bordeaux, Nouvelle-Aquitaine, France"), config)[0]
    assert paris > other


def test_seniority_soft_penalty_not_dropped(config):
    score, reasons, keep = evaluate(J(title="Senior Machine Learning Engineer"), config)
    assert keep is True
    assert "soft penalty" in reasons


def test_junior_role_survives(config):
    score, _, keep = evaluate(J(title="Junior Machine Learning Engineer",
                                desc="1-2 years experience required"), config)
    assert keep is True and score > 0
