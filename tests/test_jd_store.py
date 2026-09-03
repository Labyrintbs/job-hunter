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


def test_save_jd_overwrites_on_resave(tmp_path, monkeypatch):
    monkeypatch.setattr(jd_store, "JD_DIR", tmp_path / "jd")

    jd_store.save_jd(source="wttj", external_id="1", title="A", company="B",
                     url="", description="first version")
    path = jd_store.save_jd(source="wttj", external_id="1", title="A", company="B",
                            url="", description="second version")

    assert list((tmp_path / "jd").iterdir()) == [path]
    assert "second version" in path.read_text(encoding="utf-8")
    assert "first version" not in path.read_text(encoding="utf-8")
