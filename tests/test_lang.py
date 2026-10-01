import pytest

from jobhunter import lang

FR = ("Nous recherchons un ingénieur pour rejoindre notre équipe. Vous travaillerez avec les "
      "data scientists sur des projets de machine learning dans une entreprise en forte croissance, "
      "et vous serez en charge de la mise en production des modèles.")
EN = ("We are looking for an engineer to join our team. You will work with the data scientists "
      "on machine learning projects in a fast growing company, and you will be in charge of "
      "putting the models into production.")


def test_detects_clear_french_and_english():
    assert lang.detect(FR) == "fr"
    assert lang.detect(EN) == "en"


def test_html_is_ignored():
    assert lang.detect(f"<p><strong>{FR}</strong></p><br/>") == "fr"


def test_too_little_or_mixed_text_is_undecided():
    assert lang.detect("") == ""
    assert lang.detect("Data Scientist H/F") == ""
    assert lang.detect(FR + " " + EN) == ""


def test_english_technical_terms_do_not_make_french_text_english():
    text = ("Vous rejoindrez notre équipe pour travailler sur LangGraph, le fine-tuning de LLM et "
            "le LLM-as-a-judge avec des pipelines Docker dans une entreprise de la santé.")
    assert lang.detect(text) == "fr"


@pytest.mark.parametrize("title,description,label,expected", [
    ("AI Engineer", FR, "en", "fr"),                    # the text beats a wrong label
    ("Ingénieur IA", EN, "fr", "en"),
    ("Data scientist pour notre équipe", "", "", "fr"),  # no description: the title decides
    ("Data Scientist", "", "fr", "fr"),                  # nothing in the text: the label
    ("Data Scientist", "", "", "en"),                    # nothing at all: English
    ("Data Scientist", FR + " " + EN, "", "en"),         # mixed and unlabeled: English
])
def test_job_language(title, description, label, expected):
    assert lang.job_language(title, description, label) == expected
