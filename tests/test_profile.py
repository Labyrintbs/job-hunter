from jobhunter.llm import profile


def test_escaped_percent_not_treated_as_comment():
    # Regression: a literal '%' (LaTeX comment marker) was truncating the rest of
    # its line even when escaped as '\%' (real content, an actual percentage).
    text = profile.profile_text()
    assert "96.8% and cutting inference time by 40.8%" in text


def test_tailored_cv_text_reads_only_the_document_body_of_a_tex_file(tmp_path):
    tex = tmp_path / "cv-1.tex"
    tex.write_text(
        "\\documentclass{article}\n\\newcommand{\\secret}{PREAMBLE_ONLY}\n\\begin{document}\n"
        "\\section{SKILLS} Python, 50\\% faster % a comment\n\\end{document}\n", encoding="utf-8")

    text = profile.tailored_cv_text(tex)

    assert "SKILLS" in text and "Python, 50% faster" in text
    assert "PREAMBLE_ONLY" not in text and "a comment" not in text
