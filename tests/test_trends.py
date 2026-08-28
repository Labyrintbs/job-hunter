import csv
import io

from jobhunter import db, export, match
from jobhunter.models import Job


def J(ext, title="ML Engineer", company="Acme", loc="Paris, Ile-de-France, France"):
    return Job(source="wttj", external_id=ext, title=title, company=company, location=loc)


def test_geo_tier_classification(config):
    g = lambda loc: match.geo_tier(loc, config)
    assert g("Paris, Ile-de-France, France") == "idf"
    assert g("Bordeaux, Nouvelle-Aquitaine, France") == "france"
    assert g("Remote") == "remote"
    assert g("Full remote") == "remote"
    assert g("Télétravail, France") == "remote"   # remote wins over generic 'france'
    assert g("London, UK") == "outside"
    assert g("") == "unknown"


def test_last_seen_advances_but_fetched_at_stays(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", loc="Paris"), 60, "r", geo_tier="idf")
        first = db.get_job(conn, jid)
        assert first["geo_tier"] == "idf"
        assert first["last_seen"]
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET last_seen = '2000-01-01 00:00:00', fetched_at = '2000-01-01 00:00:00' WHERE id = ?", (jid,))
    with db.connect() as conn:
        db.upsert_job(conn, J("1", loc="Paris"), 70, "r2", geo_tier="idf")   # refetch
        row = db.get_job(conn, jid)
        assert row["fetched_at"] == "2000-01-01 00:00:00"   # first-seen preserved
        assert row["last_seen"] != "2000-01-01 00:00:00"    # bumped to now


def test_run_fetch_records_a_fetch_run(tmp_db, config):
    injected = [
        J("1", loc="Paris, Ile-de-France, France"),
        J("2", loc="Lyon, Auvergne-Rhône-Alpes, France"),
        J("3", title="Product Manager", loc="Paris"),   # not ML-relevant -> dropped
    ]
    stats = __import__("jobhunter.pipeline", fromlist=["run_fetch"]).run_fetch(config, jobs=injected)
    assert stats["fetched"] == 3 and stats["kept"] == 2
    assert stats["new_idf"] == 1 and stats["new_france"] == 1
    with db.connect() as conn:
        runs = conn.execute("SELECT * FROM fetch_runs").fetchall()
        assert len(runs) == 1
        assert runs[0]["new_idf"] == 1 and runs[0]["new_france"] == 1
        assert runs[0]["fetched"] == 3


def test_views_return_rows(tmp_db, config):
    injected = [J("1", loc="Paris, Ile-de-France, France"),
                J("2", company="BigCo", loc="Nantes, France")]
    __import__("jobhunter.pipeline", fromlist=["run_fetch"]).run_fetch(config, jobs=injected)
    with db.connect() as conn:
        by_day = conn.execute("SELECT * FROM v_new_jobs_by_day").fetchall()
        tiers = {r["geo_tier"] for r in by_day}
        assert "idf" in tiers and "france" in tiers
        companies = [r["company"] for r in conn.execute("SELECT * FROM v_top_companies")]
        assert "Acme" in companies and "BigCo" in companies
        assert conn.execute("SELECT * FROM v_market_by_run").fetchall()
        assert conn.execute("SELECT * FROM v_score_seniority_mix").fetchall()


def test_export_writes_valid_csv_and_json(tmp_db, tmp_path, config):
    __import__("jobhunter.pipeline", fromlist=["run_fetch"]).run_fetch(config, jobs=[J("1", loc="Paris")])
    csv_paths = export.export(tmp_path / "csv", view="all", fmt="csv")
    assert len(csv_paths) == len(db.VIEW_NAMES)
    top = tmp_path / "csv" / "v_top_companies.csv"
    parsed = list(csv.DictReader(io.StringIO(top.read_text())))
    assert parsed and "company" in parsed[0]

    json_paths = export.export(tmp_path / "json", view="v_new_jobs_by_day", fmt="json")
    assert len(json_paths) == 1 and json_paths[0].suffix == ".json"


def test_export_rejects_unknown_view(tmp_db):
    with db.connect() as conn:
        try:
            export.view_rows(conn, "jobs; DROP TABLE jobs")
            assert False, "should have raised"
        except ValueError:
            pass
