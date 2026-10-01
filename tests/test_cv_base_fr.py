"""The French master must mirror the English one block for block: every tailoring
decision (which entries, which bullets, which skill lines) is made on one and
applied to the other, so a missing or extra entry would silently skew a CV."""
import re
import shutil

import pytest

from jobhunter.tailor import engine, snippet_bank

BASE_FR = engine.BASE_CV.with_name("cv_base_fr.tex")


@pytest.fixture(scope="module")
def en():
    return snippet_bank.parse(engine.BASE_CV, "en")


@pytest.fixture(scope="module")
def fr():
    return snippet_bank.parse(BASE_FR, "fr")


def test_same_number_of_entries(en, fr):
    assert len(fr.projects) == len(en.projects) == 6
    assert len(fr.experiences) == len(en.experiences) == 3
    assert len(fr.skills) == len(en.skills) == 6


def test_same_bullet_counts_entry_by_entry(en, fr):
    assert [len(b.bullets()) for b in fr.experiences] == [len(b.bullets()) for b in en.experiences]
    assert [len(b.bullets()) for b in fr.projects] == [len(b.bullets()) for b in en.projects]


def test_same_dates_entry_by_entry(en, fr):
    assert [b.end_date() for b in fr.experiences] == [b.end_date() for b in en.experiences]
    assert [b.end_date() for b in fr.projects] == [b.end_date() for b in en.projects]
    assert all(b.end_date() != (0, 0) for b in fr.projects + fr.experiences)


def test_the_tagline_and_summary_block_are_found(fr):
    assert fr.heading_line and "(CDI/CDD)" in fr.heading_line
    assert "%SUMMARY-BEGIN" in fr.document and "%SUMMARY-END" in fr.document
    assert fr.lang == "fr"


def test_the_retired_project_is_not_resurrected(fr):
    assert all("Data Joker" not in p.text for p in fr.projects)


def test_french_skill_lines_have_their_category_names(fr):
    names = [c.name for c in fr.skills]
    assert "Vision par ordinateur et imagerie médicale" in names and names[-1] == "Langues"
    assert all(n and not n.endswith(" ") for n in names)


def _preamble(path):
    text = path.read_text(encoding="utf-8").split(r"\begin{document}")[0]
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("%")
             and "babel" not in l and "frenchbsetup" not in l]
    return "\n".join(lines)


def test_the_preamble_matches_apart_from_the_language_setup():
    assert _preamble(BASE_FR) == _preamble(engine.BASE_CV)


@pytest.mark.skipif(shutil.which("latexmk", path=engine._tex_env()["PATH"]) is None,
                    reason="latexmk is not installed")
def test_the_french_master_compiles(tmp_path):
    pdf = engine.compile_tex(BASE_FR.read_text(encoding="utf-8"), tmp_path, name="cv")
    assert pdf is not None, (tmp_path / "cv.compile.log").read_text()


def test_no_leftover_english_section_titles(fr):
    for title in ("EDUCATION", "PROFESSIONAL EXPERIENCE", "SKILLS", "PROJECTS"):
        assert not re.search(r"\\section\{" + title, fr.document)
