from jobhunter.tailor import engine


def test_tex_env_prepends_known_tex_paths(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = engine._tex_env()
    for p in engine._EXTRA_TEX_PATHS:
        assert p in env["PATH"]
    assert "/usr/bin:/bin" in env["PATH"]   # existing PATH preserved, not replaced


def test_tex_env_does_not_duplicate_already_present_paths(monkeypatch):
    already = engine._EXTRA_TEX_PATHS[0]
    monkeypatch.setenv("PATH", f"{already}:/usr/bin")
    env = engine._tex_env()
    assert env["PATH"].count(already) == 1
