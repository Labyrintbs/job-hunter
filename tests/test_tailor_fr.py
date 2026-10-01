"""French CVs: the French master is chosen for French postings and everything the
tailor decides (tagline, courses, skills, summary) happens in French."""
import pytest

from jobhunter.models import Job
from jobhunter.tailor import courses, engine, snippet_bank, summary

CV_FR = ("Ingénieur Machine Learning LangGraph GRPO LLM DiliTrust fine-tuning workflows "
         "extraction d'entités juridiques multilingues recalage de nuages de points 3D Data Scientist Acme")

GOOD_FR = ("Ingénieur Machine Learning avec une expérience concrète de l'ingénierie LLM, des workflows "
           "agentiques LangGraph au fine-tuning par GRPO, acquise lors d'un stage chez DiliTrust sur "
           "l'extraction d'entités juridiques multilingues. A également mené des travaux de recherche en "
           "recalage de nuages de points 3D. Cette expérience correspond au poste de Data Scientist chez Acme.")

FR_JD = ("Nous recherchons un ingénieur pour rejoindre notre équipe. Vous travaillerez avec les "
         "data scientists sur des projets de machine learning dans une entreprise en forte "
         "croissance, et vous serez en charge de la mise en production des modèles. ") * 2


def _job(title="Data Scientist", company="Acme", description="machine learning", **kw):
    return Job(source="x", external_id="1", title=title, company=company, description=description, **kw)


# --- summary in French ---------------------------------------------------------------

def test_the_french_summary_validator_accepts_a_good_text():
    assert summary.validate_summary(GOOD_FR, CV_FR, "Acme", "fr") == ""


@pytest.mark.parametrize("text,why", [
    (GOOD_FR.replace("Cette expérience correspond au poste de", "This experience fits the role of the"),
     "language"),
    (GOOD_FR.replace("Cette expérience", "Mon expérience"), "first person"),
    (GOOD_FR.replace("chez DiliTrust", "pendant cinq ans chez DiliTrust"), "contains a number"),
    (GOOD_FR.replace("Ingénieur", "Ingénieur passionné"), "hype word"),
    (GOOD_FR.replace("concrète", "concrète de 40 %"), "contains a figure"),
    (GOOD_FR.replace("chez Acme", "chez Autre"), "company not named"),
])
def test_the_french_summary_validator_rejects(text, why):
    assert why in summary.validate_summary(text, CV_FR, "Acme", "fr")


def test_english_technical_terms_do_not_make_a_french_summary_english():
    # "workflows", "fine-tuning", "LLM", "GRPO" are all English and all fine
    assert summary.language_problem(GOOD_FR, "fr") == ""


def test_hyphenated_names_and_company_names_do_not_count_as_english_words():
    # real false alarm: "LLM-as-a-judge" (as) and the company "Free-Work" (work) looked English
    text = ("Ingénieur Machine Learning avec un pipeline LangGraph évalué par un LLM-as-a-judge et du "
            "fine-tuning, candidat au poste de Data Scientist chez Free-Work.")
    assert summary.language_problem(text, "fr") == ""


def test_french_elision_in_the_closing_line():
    assert summary.closing_line("Ingénieur IA", "Acme", "fr").endswith("du poste d'Ingénieur IA chez Acme.")
    assert summary.closing_line("Data Scientist", "Acme", "fr").endswith("du poste de Data Scientist chez Acme.")
    assert summary._de("Hôte") == "d'Hôte"


def test_the_prompt_forbids_seniority_claims_and_present_tense_for_ended_internships():
    assert "junior" in summary._SYSTEM and "past tense" in summary._SYSTEM


def test_an_english_summary_with_a_few_french_proper_names_is_still_english():
    text = ("Machine Learning Engineer with an engineering degree from École Centrale de Pékin, "
            "part of the Groupe des Écoles Centrales, and hands-on LLM experience.")
    assert summary.language_problem(text, "en") == ""


def test_pick_core_and_closing_line_follow_the_language():
    assert summary.pick_core("AI", lang="fr").startswith("Ingénieur Machine Learning spécialisé")
    assert summary.pick_core("CV", lang="fr").startswith("Ingénieur Vision par ordinateur")
    assert summary.closing_line("Data Scientist (H/F)", "Acme", "fr") == \
        "Souhaite mettre cette expérience au service du poste de Data Scientist chez Acme."
    assert summary.closing_line("", "Acme", "fr") == "Souhaite mettre cette expérience au service d'un poste chez Acme."


def test_build_in_french_retries_after_a_language_mismatch_then_uses_the_french_text(monkeypatch):
    calls = []

    def fake(job, anchor, title, company, feedback="", lang="en"):
        calls.append((feedback, lang))
        return GOOD_FR.replace("Cette expérience correspond au poste de", "This experience fits the role of the") \
            if not feedback else GOOD_FR
    monkeypatch.setattr(summary, "generate_summary", fake)
    res = summary.build(_job(), "AI", CV_FR, "fr")
    assert res.text == GOOD_FR and res.reason == ""
    assert calls[0] == ("", "fr") and "English" in calls[1][0] and "French" in calls[1][0]


def test_build_in_french_falls_back_to_the_french_variant_and_reports_a_language_mismatch(monkeypatch):
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k:
                        GOOD_FR.replace("Cette expérience correspond au poste de", "This experience fits the"))
    res = summary.build(_job("Data Scientist (H/F)"), "AI", CV_FR, "fr")
    assert res.text.startswith("Ingénieur Machine Learning spécialisé dans les systèmes LLM")
    assert res.text.endswith("Souhaite mettre cette expérience au service du poste de Data Scientist chez Acme.")
    assert res.reason == summary.LANGUAGE_REASON == "tailor_language_mismatch"


def test_an_english_summary_that_came_back_in_french_is_a_language_mismatch_too(monkeypatch):
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: GOOD_FR)
    res = summary.build(_job(), "AI", "ML Engineer " + CV_FR, "en")
    assert res.reason == "tailor_language_mismatch"
    assert res.text.endswith("Looking to bring this to the Data Scientist role at Acme.")


# --- tagline, courses, tailoring -----------------------------------------------------

def test_the_french_taglines():
    assert engine._tagline("", "fr") == \
        "{Ingénieur Machine Learning (CDI/CDD), disponible immédiatement — Île-de-France, ouvert à la mobilité}"
    assert engine._tagline("PM", "fr").startswith("{Product Manager IA (CDI/CDD)")
    assert engine._tagline("", "en").startswith("{Seeking a Machine Learning role")


def test_the_french_base_is_chosen_by_language():
    assert engine.base_cv_path("fr").name == "cv_base_fr.tex"
    assert engine.base_cv_path("en").name == "cv_base.tex"
    assert engine.base_cv_path("anything-else").name == "cv_base.tex"


def test_french_course_names_are_scored_like_the_english_ones():
    modules = ["Traitement d'images", "Algorithmes d'informatique graphique 3D", "Science des données",
               "Architecture des ordinateurs", "Imagerie biologique et médicale",
               "Reconnaissance des formes pour l'analyse et l'interprétation des images",
               "Techniques avancées de vision par ordinateur"]
    kept = courses.choose(modules, "ingénieur vision par ordinateur, segmentation d'images et nuages de points")
    assert len(kept) == courses.KEEP
    assert "Traitement d'images" in kept and "Algorithmes d'informatique graphique 3D" in kept
    assert "Architecture des ordinateurs" not in kept


def test_courses_apply_rewrites_the_french_label_too():
    doc = r"\resumeItem{\textit{Enseignements principaux} : A, B, C, D, E, F}"
    assert courses.apply(doc, "x").count(",") == courses.KEEP - 1


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: False)


def test_a_french_tailoring_uses_the_french_master_throughout(no_llm):
    job = _job("Ingénieur Vision par ordinateur", "Acme", "segmentation d'images médicales, nuages de points")
    tex = engine.tailor_tex(job, role_category="CV", language="fr")
    assert r"\section{FORMATION}" in tex and r"\section{EXPÉRIENCE PROFESSIONNELLE}" in tex
    assert r"\section{PROJETS ET RECHERCHE}" in tex and r"\section{COMPÉTENCES}" in tex
    assert r"\section{EDUCATION}" not in tex
    assert "{Ingénieur Machine Learning (CDI/CDD), disponible immédiatement" in tex
    assert "Ingénieur Vision par ordinateur ayant déployé" in tex          # the CV variant, in French
    assert tex.count("%SUMMARY-BEGIN") == 1
    line = next(l for l in tex.splitlines() if "Enseignements principaux" in l)
    assert line.count(",") == courses.KEEP - 1


def test_a_french_tailoring_drops_the_vision_skills_line_for_a_job_with_no_vision_signal(no_llm):
    tex = engine.tailor_tex(_job("Ingénieur backend", "Acme", "api web et bases de données"), language="fr")
    assert "Vision par ordinateur et imagerie médicale" not in tex.split(r"\section{COMPÉTENCES}")[1]


def test_the_llm_selection_works_on_the_french_blocks(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    parsed = snippet_bank.parse(engine.base_cv_path("fr"), "fr")
    skills = [c.name for c in parsed.skills]
    monkeypatch.setattr(engine.llm_select, "select", lambda *a, **k: {
        "experience_ids": [0], "experience_bullets": [[0, 1]], "project_ids": [0],
        "project_bullets": [[0]], "skill_categories": skills[:2], "reasoning": ""})
    tex = engine.tailor_tex(_job(), parsed=parsed)
    exp = tex.split(r"\section{EXPÉRIENCE PROFESSIONNELLE}")[1].split(r"\section{PROJETS")[0]
    assert exp.count(r"\resumeItem{") == 2 and "DiliTrust" in exp and "DeepWise" not in exp


def test_tailor_job_picks_the_master_from_the_postings_language(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    seen = []

    def fake_compile(tex, out_dir, name="cv", expected_pages=None):
        seen.append(tex)
        return None
    monkeypatch.setattr(engine, "compile_tex", fake_compile)
    engine.tailor_job(_job(description=FR_JD), 1)
    engine.tailor_job(_job(description="We are looking for an engineer to join our team. " * 10), 2)
    engine.tailor_job(_job(description=FR_JD), 3, language="en")        # an explicit choice wins
    assert r"\section{FORMATION}" in seen[0]
    assert r"\section{EDUCATION}" in seen[1]
    assert r"\section{EDUCATION}" in seen[2]


# --- review notes and the final language check ----------------------------------------

def _fake_compile(tex, out_dir, name="cv", expected_pages=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.tex").write_text(tex, encoding="utf-8")
    pdf = out_dir / f"{name}.pdf"
    pdf.write_bytes(b"%PDF")
    return pdf


def test_the_assembled_cv_language_check(no_llm):
    en_tex = engine.tailor_tex(_job(description="machine learning"), language="en")
    fr_tex = engine.tailor_tex(_job(description="machine learning"), language="fr")
    assert engine.cv_language_problem(en_tex, "en") == ""
    assert engine.cv_language_problem(fr_tex, "fr") == ""
    assert "reads as EN" in engine.cv_language_problem(en_tex, "fr")
    assert "reads as FR" in engine.cv_language_problem(fr_tex, "en")


def test_a_cv_in_the_wrong_language_gets_a_review_note_and_is_tracked(tmp_path, monkeypatch, no_llm):
    from jobhunter import fetch_diag
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _fake_compile)
    french = engine.tailor_tex(_job(), language="fr")
    monkeypatch.setattr(engine, "_tailor", lambda *a, **k: (french, False, []))
    with fetch_diag.run_tracking() as tracker:
        res = engine.tailor_job(_job(), 1, language="en")
    assert res.pdf_path is not None and res.note.startswith("language check")
    assert dict(tracker.counts) == {("tailor", "Acme", "tailor_language_mismatch"): 1}


def test_a_matching_language_leaves_no_note(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _fake_compile)
    res = engine.tailor_job(_job(description=FR_JD), 1)
    assert res.lang == "fr" and "language check" not in res.note   # (the keyword fallback note is expected here)


def test_a_summary_that_fell_back_shows_as_a_review_note(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    monkeypatch.setattr(engine, "compile_tex", _fake_compile)
    monkeypatch.setattr(summary, "generate_summary", lambda *a, **k: "Too short.")
    res = engine.tailor_job(_job(), 1)
    assert "summary fell back to the standard text (" in res.note


# --- fitting two pages: French length hint, last-resort trimming, voice ------------------

def test_french_selection_is_told_that_french_runs_longer(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    seen = {}

    def fake(job, e, p, s, feedback=None, judge_context=None):
        seen["feedback"] = feedback
        return {"experience_ids": [0], "experience_bullets": [[]], "project_ids": [0],
                "project_bullets": [[]], "skill_categories": ["Langues"], "reasoning": ""}
    monkeypatch.setattr(engine.llm_select, "select", fake)
    parsed = snippet_bank.parse(engine.base_cv_path("fr"), "fr")
    engine.tailor_tex(_job(), parsed=parsed)
    assert "French version" in seen["feedback"] and "10% longer" in seen["feedback"]
    parsed_en = snippet_bank.parse(engine.base_cv_path("en"), "en")
    engine.tailor_tex(_job(), parsed=parsed_en)
    assert seen["feedback"] is None                     # English gets no hint


def test_the_hint_is_added_to_a_retry_feedback_not_replaced(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: True)
    seen = {}
    monkeypatch.setattr(engine.llm_select, "select", lambda job, e, p, s, feedback=None, judge_context=None:
                        seen.update(feedback=feedback) or {})
    parsed = snippet_bank.parse(engine.base_cv_path("fr"), "fr")
    engine.tailor_tex(_job(), parsed=parsed, feedback="compiled to 3 pages")
    assert seen["feedback"].startswith("compiled to 3 pages") and "French version" in seen["feedback"]


def test_drop_last_project_bullet_trims_the_last_multi_bullet_project_and_keeps_one_each():
    parsed = snippet_bank.parse(engine.BASE_CV, "en")
    doc = parsed.document
    before = [len(b.bullets()) for b in snippet_bank.parse(engine.BASE_CV, "en").projects]
    once = snippet_bank.drop_last_project_bullet(doc, "en")
    after = [len(b.bullets()) for b in _projects_of(once)]
    assert sum(before) - sum(after) == 1
    assert after[-1] == before[-1] - 1 and after[:-1] == before[:-1]    # the last project (thesis) lost one
    doc2 = doc
    for _ in range(40):
        nxt = snippet_bank.drop_last_project_bullet(doc2, "en")
        if nxt is None:
            break
        doc2 = nxt
    left = _projects_of(doc2)
    assert all(len(b.bullets()) <= 1 for b in left)           # bullets first...
    assert len(left) == snippet_bank.MIN_PROJECTS             # ...then whole projects, down to the floor
    assert snippet_bank.drop_last_project_bullet(doc2, "en") is None   # nothing left to drop


def _projects_of(doc, lang="en"):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.tex"
        p.write_text(doc, encoding="utf-8")
        return snippet_bank.parse(p, lang).projects


def _page_count_log(out_dir, name, pages=3):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.compile.log").write_text(f"Compiled to {pages} page(s), expected exactly 2.\n")


def test_a_cv_still_over_two_pages_after_the_retry_is_trimmed_until_it_fits(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    compiles = []

    def fake_compile(tex, out_dir, name="cv", expected_pages=None):
        compiles.append(tex)
        if len(compiles) < 4:                       # first attempt, retry, first trim: still 3 pages
            _page_count_log(out_dir, name)
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.tex").write_text(tex)
        (out_dir / f"{name}.pdf").write_bytes(b"%PDF")
        return out_dir / f"{name}.pdf"
    monkeypatch.setattr(engine, "compile_tex", fake_compile)
    res = engine.tailor_job(_job(), 1, auto=True)
    assert res.pdf_path is not None
    assert "auto-trimmed 2 project item(s) to fit two pages" in res.note
    assert len(compiles) == 4
    assert compiles[3].count(r"\resumeItem{") == compiles[1].count(r"\resumeItem{") - 2


def test_trimming_gives_up_after_the_cap_and_leaves_the_page_count_failure(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    compiles = []

    def always_three_pages(tex, out_dir, name="cv", expected_pages=None):
        compiles.append(tex)
        _page_count_log(out_dir, name)
        return None
    monkeypatch.setattr(engine, "compile_tex", always_three_pages)
    res = engine.tailor_job(_job(), 1, auto=True)
    assert res.pdf_path is None and "3 page(s)" in res.note and "auto-trimmed" not in res.note
    assert 2 < len(compiles) <= 2 + engine.MAX_AUTOTRIM      # attempt, retry, then trims up to the cap or the floor


def test_a_latex_error_is_never_trimmed(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    compiles = []

    def broken(tex, out_dir, name="cv", expected_pages=None):
        compiles.append(1)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.compile.log").write_text("! Undefined control sequence.\n")
        return None
    monkeypatch.setattr(engine, "compile_tex", broken)
    res = engine.tailor_job(_job(), 1, auto=True)
    assert res.pdf_path is None and "LaTeX compile error" in res.note and len(compiles) == 1


def test_the_interactive_path_is_not_trimmed(tmp_path, monkeypatch, no_llm):
    monkeypatch.setattr(engine, "CV_OUT_DIR", tmp_path)
    compiles = []
    monkeypatch.setattr(engine, "compile_tex", lambda tex, out_dir, name="cv", expected_pages=None:
                        compiles.append(1) or None)
    engine.tailor_job(_job(), 1, auto=False)
    assert len(compiles) == 1


def test_the_french_summary_voice_has_no_third_person_pronoun():
    ok = GOOD_FR
    assert summary.validate_summary(ok, CV_FR, "Acme", "fr") == ""
    bad = ok.replace("Cette expérience correspond", "Il vise")
    assert "third-person pronoun" in summary.validate_summary(bad, CV_FR, "Acme", "fr")
    assert "noun-phrase voice" in summary._LANGUAGE_RULE["fr"]
