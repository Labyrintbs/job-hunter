import pytest

from jobhunter.models import Job
from jobhunter.tailor import engine, snippet_bank, summary

CV_TEXT = ("Machine Learning Engineer ML LangGraph GRPO fine-tuning LLM LLM-as-judge CT CTA segmentation Open3D PyTorch "
           "3D point cloud registration DiliTrust multilingual legal extraction")

GOOD = ("Machine Learning Engineer with hands-on LLM engineering experience, from LangGraph agentic "
        "pipelines to GRPO fine-tuning, gained during an internship at DiliTrust on multilingual legal "
        "extraction. Also researched 3D point cloud registration. This background fits the ML Engineer role at Acme.")


def _job(title="ML Engineer", company="Acme", description="machine learning"):
    return Job(source="x", external_id="1", title=title, company=company, description=description)


@pytest.mark.parametrize("category,expected", [
    ("AI", "LLM systems"), ("NLP", "LLM systems"), ("CV", "Computer Vision Engineer"),
    ("ML/DL", "hands-on LLM engineering"), ("PM", "hands-on LLM engineering"),
    ("", "hands-on LLM engineering"), ("unheard-of", "hands-on LLM engineering"),
])
def test_pick_core_follows_the_role_category(category, expected):
    assert expected in summary.pick_core(category)


@pytest.mark.parametrize("raw,clean", [
    ("Data Scientist (H/F)", "Data Scientist"),
    ("Data scientist - F/H", "Data scientist"),
    ("AI Engineer (f/m/d)", "AI Engineer"),
    ("Machine Learning Engineer - H/F/NB", "Machine Learning Engineer"),
    ("CDI - GenAI Engineer", "GenAI Engineer"),
    ("Machine Learning - ai Engineer - Industrie & 3D H/F", "AI Engineer"),
    ("Computer Vision Engineer, 3D Perception | Tier 1 VC-backed Startup", "Computer Vision Engineer, 3D Perception"),
    ("Data Scientist: France", "Data Scientist"),
    ("AI Platform &amp; MLOps Engineer (M/F) (F/H)", "AI Platform & MLOps Engineer"),
    ("Research Engineer – Machine Learning", "Research Engineer"),
    ("Design Develop - Llm Models", "Design Develop"),
    ("x" * 61, ""),
    ("", ""),
])
def test_clean_title(raw, clean):
    assert summary.clean_title(raw) == clean


@pytest.mark.parametrize("raw,clean", [
    ("Doctrine", "Doctrine"), ("Non renseigné", ""), ("Confidential", ""), ("AT&amp;T", "AT&T"),
    ("A very long company name that goes on and on Ltd", ""), ("", ""),
])
def test_clean_company(raw, clean):
    assert summary.clean_company(raw) == clean


def test_closing_line_uses_whatever_is_clean():
    assert summary.closing_line("ML Engineer (H/F)", "Acme") == \
        "Looking to bring this to the ML Engineer role at Acme."
    assert summary.closing_line("ML Engineer", "Non renseigné") == "Looking to bring this to the ML Engineer role."
    assert summary.closing_line("x" * 80, "Acme") == "Looking to bring this to a role at Acme."
    assert summary.closing_line("", "") == ""


def test_latex_escape():
    assert summary.latex_escape("R&D 50% #1 a_b") == r"R\&D 50\% \#1 a\_b"


def test_a_natural_summary_about_cv_facts_is_accepted():
    assert summary.validate_summary(GOOD, f"{CV_TEXT} ML Engineer Acme", "Acme") == ""


def test_a_number_or_name_the_cv_contains_is_not_flagged():
    # regressions from a real run: "3D" has a digit, "LLMs" is the CV's "LLM", and
    # "Targeting"/"Now" merely open a sentence
    s = GOOD.replace("Also researched", "Now researching LLMs and")
    assert summary.validate_summary(s, f"{CV_TEXT} Acme", "Acme") == ""


def _with(text):
    return GOOD.replace("Also researched 3D point cloud registration.", text)


@pytest.mark.parametrize("text,why", [
    ("", "empty"),
    (None, "empty"),
    ("Too short.", "words"),
    (GOOD + " " + "More words here. " * 10, "words"),
    (_with("Also five years of point cloud registration."), "contains a number"),
    (_with("Also 12 papers on registration."), "contains a figure"),
    (_with("Also cut inference time by 40.8% in evaluation."), "contains a figure"),
    (_with("Also tuned a 7B model."), "unsupported number 7B"),
    (_with("My research covered registration."), "first person"),
    (_with("Passionate about point cloud registration."), "hype word"),
    (_with("Also researched registration, and more, a lot more — really."), "dash"),
    (_with("Also researched registration with Kubernetes clusters."), "unsupported term kubernetes"),
    (_with("Also researched registration at Google."), "unsupported term Google"),
    (_with("Also researched registration.\nAnd more."), "multi-line"),
    (GOOD.replace("Acme", "Other Corp"), "company not named"),
    (GOOD + " One. Two. Three.", "too many sentences"),
])
def test_validate_rejects_what_the_cv_does_not_support(text, why):
    assert why in summary.validate_summary(text, f"{CV_TEXT} ML Engineer Acme", "Acme")


def test_a_tool_the_cv_really_has_is_allowed_even_if_normally_unclaimed():
    assert summary.validate_summary(_with("Also ran Kubernetes clusters."), CV_TEXT + " Kubernetes Acme", "Acme") == ""


def test_any_real_word_of_the_company_name_counts():
    s = GOOD.replace("Acme", "Terabase")
    assert summary.validate_summary(s, CV_TEXT + " Terabase", "TERABASE ENERGY INC") == ""


def test_build_uses_a_valid_llm_summary(monkeypatch):
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: GOOD)
    res = summary.build(_job(), "AI", CV_TEXT)
    assert res.text == GOOD
    assert res.reason == ""


def test_build_retries_once_with_the_rejection_reason(monkeypatch):
    calls = []

    def fake(job, anchor, title, company, feedback="", **k):
        calls.append(feedback)
        return "Too short." if not feedback else GOOD
    monkeypatch.setattr(summary, "generate_summary", fake)
    res = summary.build(_job(), "AI", CV_TEXT)
    assert res.text == GOOD and res.reason == ""
    assert calls[0] == "" and "words" in calls[1]


def test_build_falls_back_after_two_rejections_and_reports_why(monkeypatch):
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: "Too short.")
    res = summary.build(_job("Data Scientist (H/F)"), "AI", CV_TEXT)
    assert res.text.startswith("Machine Learning Engineer specialized in LLM systems")
    assert res.text.endswith("Looking to bring this to the Data Scientist role at Acme.")
    assert res.reason == "tailor_summary_rejected"
    assert "words" in res.detail


def test_build_falls_back_and_reports_when_the_llm_call_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(summary, "generate_summary", boom)
    res = summary.build(_job(), "CV", CV_TEXT)
    assert res.text.startswith("Computer Vision Engineer")
    assert res.reason == "tailor_summary_llm_error"


def test_build_without_a_backend_uses_the_fallback_and_reports_nothing_extra(monkeypatch):
    # the selection call already tracks "no LLM backend"; the summary must not repeat it
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: None)
    res = summary.build(_job(), "AI", CV_TEXT)
    assert res.text.endswith("Looking to bring this to the ML Engineer role at Acme.")
    assert res.reason == ""


def test_the_variants_file_is_well_formed():
    data = summary.load_variants()
    assert set(data["variants"]) == {"llm", "cv", "general"}
    for v in data["variants"].values():
        assert len(v["core"].split()) <= 40
        assert "{" not in v["core"]


def test_set_summary_replaces_or_removes_the_marked_block():
    doc = "a\n%SUMMARY-BEGIN\nold\n%SUMMARY-END\nb"
    assert "\\noindent\\small{New text.}" in snippet_bank.set_summary(doc, "New text.")
    assert "old" not in snippet_bank.set_summary(doc, "New text.")
    assert snippet_bank.set_summary(doc, "") == "a\n%SUMMARY-BEGIN\n%SUMMARY-END\nb"
    assert snippet_bank.set_summary("no markers", "x") == "no markers"


def test_base_cv_summary_block_matches_the_general_variant():
    # the base file must compile on its own, so its block is the general anchor
    parsed = snippet_bank.parse(engine.BASE_CV)
    assert summary.pick_core("") in parsed.document
    assert "%SUMMARY-BEGIN" in parsed.document and "%SUMMARY-END" in parsed.document


def test_tailoring_puts_the_summary_above_education_and_keeps_the_tagline(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    text = GOOD.replace("Acme", "Acme & Co")
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: text)
    job = _job("Vision Engineer", "Acme & Co", "computer vision segmentation")
    tex = engine.tailor_tex(job, role_category="CV")
    assert tex.index("Seeking a Machine Learning role") < tex.index("hands-on LLM engineering experience")
    assert tex.index("hands-on LLM engineering experience") < tex.index("\\section{EDUCATION}")
    assert r"Acme \& Co" in tex
    assert tex.count("%SUMMARY-BEGIN") == 1
