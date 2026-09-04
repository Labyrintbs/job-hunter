from jobhunter.match import (classify_role, detect_seniority, has_citizenship_requirement,
                              is_relevant, min_years_required, screen)
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


def test_freelance_contract_type_excluded(config):
    """Regression: HelloWork (and other French sources) report contract type as
    'Indépendant', not the English 'freelance' -- both must be caught."""
    job = J(contract="Indépendant")
    s = screen(job, config)
    assert s.keep is False and "excluded" in s.filter_reason


def test_portage_salarial_excluded(config):
    job = J(contract="Portage salarial")
    assert screen(job, config).keep is False


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


def test_classify_role_nlp(config):
    assert classify_role("NLP Engineer", "", config) == "NLP"


def test_classify_role_cv(config):
    assert classify_role("Computer Vision Engineer", "", config) == "CV"


def test_classify_role_ai(config):
    assert classify_role("AI Engineer", "", config) == "AI"


def test_classify_role_ml_dl_default(config):
    assert classify_role("Machine Learning Engineer", "", config) == "ML/DL"
    assert classify_role("Data Scientist", "", config) == "ML/DL"


def test_classify_role_falls_back_to_description(config):
    # generic title, domain signal only shows up in the body
    assert classify_role("Software Engineer", "We work on object detection models.", config) == "CV"


def test_classify_role_title_takes_priority_over_description(config):
    # title is NLP-specific even though the body mentions a generic ML term too
    assert classify_role("NLP Engineer", "great machine learning team", config) == "NLP"


def test_screen_surfaces_role_category(config):
    s = screen(J(title="Computer Vision Engineer"), config)
    assert s.role_category == "CV"


# --- min_years_required: "years working on X" phrasing without the word "experience" ---

def test_min_years_catches_years_working_on_phrasing():
    # Real posting that used to evade the seniority gate entirely (min_years == None)
    # because the old regex required "experience"/"expérience" to co-occur with the number.
    assert min_years_required("4 + years working on large-scale ml codebases.") == 4


def test_min_years_still_ignores_company_age():
    assert min_years_required("founded 8 years ago in paris") is None
    assert min_years_required("we have been building this company for 10 years") is None


def test_min_years_still_catches_experience_phrasing():
    assert min_years_required("environ 3 ans d'expérience en machine learning") == 3
    assert min_years_required("proven track record... 2+ years of experience in ai/ml") == 2


# --- citizenship / eligibility hard gate ---

def test_citizenship_requirement_detected():
    assert has_citizenship_requirement(
        "eligibility: must hold citizenship in the target territory (france for now)."
    ) is True


def test_citizenship_requirement_not_confused_with_generic_clearance_language():
    # "security clearance" alone is a softer signal, not the explicit-citizenship red line.
    assert has_citizenship_requirement(
        "clearable: must meet all local requirements for high-level security clearance."
    ) is False


def test_screen_filters_job_with_citizenship_requirement(config):
    s = screen(J(desc="eligibility: must hold citizenship in the target territory (france)."),
               config)
    assert s.keep is True             # still stored, reviewable
    assert s.filtered is True
    assert "citizenship" in s.filter_reason


def test_citizenship_gate_can_be_disabled_via_config(config):
    cfg = {**config, "citizenship_gate": False}
    s = screen(J(desc="must hold citizenship in the target territory."), cfg)
    assert s.filtered is False


# --- client-facing / consulting soft signal ---

def test_client_facing_language_penalizes_score(config):
    plain = screen(J(title="AI Engineer", desc="build LLM applications"), config).score
    forward_deployed = screen(
        J(title="Applied AI Engineer", desc="build LLM applications, forward deployed"), config
    ).score
    assert forward_deployed < plain


def test_client_facing_reason_surfaced(config):
    s = screen(J(desc="you'll join pre-sales calls with our customers"), config)
    assert any("client-facing" in r for r in s.reasons.split("; "))
