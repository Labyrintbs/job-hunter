from jobhunter import db, market_trend


def _config(**overrides):
    cfg = {"market_trend": {"enabled": True, **overrides}}
    return cfg


def test_snapshot_records_one_row_per_category_and_scope(tmp_db, monkeypatch):
    calls = []

    def fake_count(departements=None, **filters):
        calls.append((departements, filters))
        return 42

    monkeypatch.setattr(market_trend.francetravail, "count", fake_count)

    result = market_trend.snapshot(_config())

    assert result == {"recorded": len(market_trend.DEFAULT_CATEGORIES) * 2, "failed": []}
    # one nationwide (departements=None) + one idf-scoped call per category
    assert len(calls) == len(market_trend.DEFAULT_CATEGORIES) * 2
    nationwide = [c for c in calls if c[0] is None]
    idf = [c for c in calls if c[0] is not None]
    assert len(nationwide) == len(idf) == len(market_trend.DEFAULT_CATEGORIES)


def test_snapshot_writes_expected_rows(tmp_db, monkeypatch):
    monkeypatch.setattr(market_trend.francetravail, "count", lambda departements=None, **f: 100)

    market_trend.snapshot(_config())

    with db.connect() as conn:
        rows = conn.execute("SELECT scope, category, total_count FROM market_snapshots").fetchall()
    scopes = {r["scope"] for r in rows}
    categories = {r["category"] for r in rows}
    assert scopes == {"france", "idf"}
    assert categories == {c["label"] for c in market_trend.DEFAULT_CATEGORIES}
    assert all(r["total_count"] == 100 for r in rows)


def test_snapshot_uses_configured_idf_departements(tmp_db, monkeypatch):
    seen_departements = []

    def fake_count(departements=None, **filters):
        seen_departements.append(departements)
        return 1

    monkeypatch.setattr(market_trend.francetravail, "count", fake_count)

    market_trend.snapshot(_config(idf_departements="75,92"))

    assert "75,92" in seen_departements
    assert None in seen_departements   # the nationwide pass


def test_snapshot_passes_domaine_and_coderome_as_filters(tmp_db, monkeypatch):
    seen_filters = []

    def fake_count(departements=None, **filters):
        seen_filters.append(filters)
        return 1

    monkeypatch.setattr(market_trend.francetravail, "count", fake_count)

    market_trend.snapshot(_config())

    assert {"domaine": "M18"} in seen_filters
    assert {"codeROME": "M1889"} in seen_filters   # AI engineering


def test_snapshot_skips_failed_counts_without_writing_a_row(tmp_db, monkeypatch):
    monkeypatch.setattr(market_trend.francetravail, "count", lambda departements=None, **f: None)

    result = market_trend.snapshot(_config())

    assert result["recorded"] == 0
    assert len(result["failed"]) == len(market_trend.DEFAULT_CATEGORIES) * 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 0


def test_snapshot_disabled_writes_nothing(tmp_db, monkeypatch):
    monkeypatch.setattr(market_trend.francetravail, "count",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))

    result = market_trend.snapshot(_config(enabled=False))

    assert result == {"recorded": 0, "failed": []}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 0


def test_snapshot_uses_custom_categories_when_configured(tmp_db, monkeypatch):
    monkeypatch.setattr(market_trend.francetravail, "count", lambda departements=None, **f: 7)

    custom = _config(categories=[{"label": "Custom", "param": "codeROME", "code": "M9999"}])
    result = market_trend.snapshot(custom)

    assert result == {"recorded": 2, "failed": []}   # 1 category x 2 scopes
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM market_snapshots").fetchall()
    assert [r["category"] for r in rows] == ["Custom"]
