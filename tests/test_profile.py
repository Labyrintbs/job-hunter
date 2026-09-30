from jobhunter.llm import profile


def test_escaped_percent_not_treated_as_comment():
    # Regression: a literal '%' (LaTeX comment marker) was truncating the rest of
    # its line even when escaped as '\%' (real content, an actual percentage).
    text = profile.profile_text()
    assert "96.8% and cutting inference time by 40.8%" in text
