from jobhunter.match import detect_seniority, is_relevant, min_years_required, screen
from jobhunter.models import Job


def J(title="Machine Learning Engineer", desc="", loc="Paris, Ile-de-France, France",
      contract="FULL_TIME", company="Acme", lang="fr"):
    return Job(source="wttj", external_id="1", title=title, company=company,
               location=loc, description=desc, contract_type=contract, language=lang)


def test_internship_excluded(config):
    s = screen(J(title="Stage - Machine Learning"), config)
    assert s.keep is False and "excluded" in s.filter_reason


def test_alternance_excluded(config):
    assert screen(J(title="Alternance Data Scientist"), config).keep is False


def test_non_ml_title_dropped_even_with_ml_boilerplate(config):
    job = J(title="Product Manager", desc="We are a machine learning company using pytorch.")
    assert is_relevant(job, config) is False
    assert screen(job, config).keep is False


def test_ml_title_relevant(config):
    assert is_relevant(J(title="Senior Machine Learning Engineer"), config) is True
    assert is_relevant(J(title="Data Scientist"), config) is True
    assert is_relevant(J(title="AI Engineer"), config) is True


def test_paris_ranks_above_other_france(config):
    paris = screen(J(loc="Paris, Ile-de-France, France"), config).score
    other = screen(J(loc="Bordeaux, Nouvelle-Aquitaine, France"), config).score
    assert paris > other


def test_senior_title_goes_to_filtered_bucket(config):
    s = screen(J(title="Senior Machine Learning Engineer"), config)
    assert s.keep is True            # still stored
    assert s.filtered is True        # but auto-hidden
    assert s.seniority == "senior"
    assert "senior" in s.filter_reason


def test_junior_title_survives_even_with_years(config):
    s = screen(J(title="Junior Machine Learning Engineer",
                 desc="5+ years of experience required"), config)
    assert s.keep is True and s.filtered is False   # junior title overrides the gate
    assert s.seniority == "junior"


def test_too_many_years_filtered(config):
    s = screen(J(title="Machine Learning Engineer",
                 desc="We need at least 6 years of experience in production ML."), config)
    assert s.filtered is True and s.min_years == 6


def test_min_years_parser():
    assert min_years_required("5+ years of experience") == 5
    assert min_years_required("3-5 years experience required") == 3
    assert min_years_required("minimum 4 years") == 4
    assert min_years_required("au moins 2 ans d'expérience") == 2
    assert min_years_required("no requirement here") is None
    assert min_years_required("founded 8 years ago, great culture") is None  # not experience


def test_detect_seniority():
    assert detect_seniority(J(title="Lead ML Engineer")) == "senior"
    assert detect_seniority(J(title="Staff Data Scientist")) == "senior"
    assert detect_seniority(J(title="Junior AI Engineer")) == "junior"
    assert detect_seniority(J(title="Machine Learning Engineer")) == "unknown"


def test_low_score_filtered(config):
    # ML-relevant by title but weak match + outside France -> below min_score -> Filtered, not dropped.
    s = screen(J(title="Research Scientist", desc="", loc="London, UK"), config)
    assert s.keep is True and s.filtered is True
    assert s.score < config["min_score"]


def test_active_negative_kw_rule_filters(config):
    cfg = {**config, "_active_rules": [{"id": 7, "kind": "negative_kw", "value": "blockchain"}]}
    s = screen(J(title="Machine Learning Engineer", desc="we build on blockchain"), cfg)
    assert s.filtered is True and 7 in s.matched_rules and "rule: 'blockchain'" in s.filter_reason


def test_active_company_block_rule_filters(config):
    cfg = {**config, "_active_rules": [{"id": 3, "kind": "company_block", "value": "badcorp"}]}
    s = screen(J(company="BadCorp"), cfg)
    assert s.filtered is True and 3 in s.matched_rules


def test_no_active_rules_leaves_job_alone(config):
    s = screen(J(title="Machine Learning Engineer", desc="great role"), config)
    assert s.filtered is False and s.matched_rules == []
