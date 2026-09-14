from jobhunter import jd_store


def test_save_jd_writes_header_and_description(tmp_path, monkeypatch):
    monkeypatch.setattr(jd_store, "JD_DIR", tmp_path / "jd")

    path = jd_store.save_jd(source="linkedin", external_id="123", title="ML Engineer",
                            company="Acme", url="http://x/123", description="full JD text here")

    assert path == tmp_path / "jd" / "linkedin__123.txt"
    content = path.read_text(encoding="utf-8")
    assert "source: linkedin" in content
    assert "external_id: 123" in content
    assert "title: ML Engineer" in content
    assert "company: Acme" in content
    assert "url: http://x/123" in content
    assert content.endswith("full JD text here")


def test_save_jd_sanitizes_url_shaped_external_id(tmp_path, monkeypatch):
    # Regression: manually-imported jobs (pipeline.import_manual_job) use the full
    # posting URL as external_id -- "/" and ":" in it were being read as path
    # separators, crashing save_jd with FileNotFoundError on the nested "directory".
    monkeypatch.setattr(jd_store, "JD_DIR", tmp_path / "jd")

    path = jd_store.save_jd(source="manual", external_id="https://example.com/jobs/123?ref=a",
                            title="Data Scientist", company="Acme", url="https://example.com/jobs/123",
                            description="full JD text")

    assert path.parent == tmp_path / "jd"   # not written into a nested subdirectory
    assert path.exists()
    assert "external_id: https://example.com/jobs/123?ref=a" in path.read_text(encoding="utf-8")


def test_save_jd_overwrites_on_resave(tmp_path, monkeypatch):
    monkeypatch.setattr(jd_store, "JD_DIR", tmp_path / "jd")

    jd_store.save_jd(source="wttj", external_id="1", title="A", company="B",
                     url="", description="first version")
    path = jd_store.save_jd(source="wttj", external_id="1", title="A", company="B",
                            url="", description="second version")

    assert list((tmp_path / "jd").iterdir()) == [path]
    assert "second version" in path.read_text(encoding="utf-8")
    assert "first version" not in path.read_text(encoding="utf-8")
