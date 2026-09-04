from jobhunter.llm import profile


def test_condensed_profile_drops_projects_section():
    text = profile.condensed_profile_text()
    assert "PROJECTS" not in text
    # A phrase that only appears inside a project bullet, not experience/skills.
    assert "Point Cloud" not in text
    assert "NeRF" not in text


def test_condensed_profile_keeps_background_and_skills():
    text = profile.condensed_profile_text()
    assert "EDUCATION" in text
    assert "PROFESSIONAL EXPERIENCE" in text
    assert "DiliTrust" in text
    assert "SKILLS" in text
    assert "LangGraph" in text


def test_condensed_profile_shorter_than_full():
    assert len(profile.condensed_profile_text()) < len(profile.profile_text())


def test_escaped_percent_not_treated_as_comment():
    # Regression: a literal '%' (LaTeX comment marker) was truncating the rest of
    # its line even when escaped as '\%' (real content, an actual percentage).
    text = profile.profile_text()
    assert "96.8% and cutting inference time by 40.8%" in text
