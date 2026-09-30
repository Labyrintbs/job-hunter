from jobhunter.config import add_company, load_companies

_YAML = """# header comment that must survive
companies:
  - { name: Dataiku, ats: greenhouse, token: dataiku }
"""


def test_add_company_appends_an_entry_and_keeps_comments(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(_YAML, encoding="utf-8")

    assert add_company("Scaleway", "lever", "scaleway", path=path) is True

    assert path.read_text(encoding="utf-8").startswith("# header comment that must survive")
    assert load_companies(path)[-1] == {"name": "Scaleway", "ats": "lever", "token": "scaleway"}
    assert len(load_companies(path)) == 2


def test_add_company_skips_a_name_already_present_case_insensitively(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(_YAML, encoding="utf-8")

    assert add_company("DATAIKU", "lever", "dataiku", path=path) is False

    assert path.read_text(encoding="utf-8") == _YAML


def test_add_company_handles_a_file_without_a_trailing_newline(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(_YAML.rstrip("\n"), encoding="utf-8")

    add_company("Scaleway", "lever", "scaleway", path=path)

    assert [c["name"] for c in load_companies(path)] == ["Dataiku", "Scaleway"]


def test_add_company_quotes_names_with_yaml_special_characters(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(_YAML, encoding="utf-8")

    add_company('Acme, Inc: "Labs" {EU}', "greenhouse", "acme", path=path)

    assert load_companies(path)[-1]["name"] == 'Acme, Inc: "Labs" {EU}'
