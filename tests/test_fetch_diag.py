import json

from jobhunter import db, fetch_diag


def test_track_is_a_noop_with_no_active_run():
    fetch_diag.track("workday", "non_france", detail="Berlin")  # must not raise
    assert fetch_diag._current is None


def test_run_tracking_counts_and_caps_samples():
    with fetch_diag.run_tracking() as t:
        for i in range(5):
            fetch_diag.track("workday", "non_france", detail=f"City {i}", company="Valeo")
        fetch_diag.track("wttj", "pagination_cap_hit", detail="ai engineer")
    assert t.counts[("workday", "Valeo", "non_france")] == 5
    assert t.samples[("workday", "Valeo", "non_france")] == ["City 0", "City 1", "City 2"]
    assert t.counts[("wttj", "", "pagination_cap_hit")] == 1


def test_run_tracking_restores_previous_tracker_on_exit():
    with fetch_diag.run_tracking() as outer:
        with fetch_diag.run_tracking() as inner:
            fetch_diag.track("hellowork", "page_fetch_failed")
        assert inner.counts[("hellowork", "", "page_fetch_failed")] == 1
        assert fetch_diag._current is outer
        fetch_diag.track("hellowork", "page_fetch_failed")
    assert outer.counts[("hellowork", "", "page_fetch_failed")] == 1
    assert fetch_diag._current is None


def test_flush_writes_rows_and_recent_fetch_drops_reads_them_back(tmp_db):
    with fetch_diag.run_tracking() as t:
        fetch_diag.track("workday", "non_france", detail="Berlin", company="Valeo")
        fetch_diag.track("workday", "non_france", detail="Munich", company="Valeo")
    with db.connect() as conn:
        t.flush(conn)
        conn.commit()
        rows = db.recent_fetch_drops(conn, hours=24)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "workday" and row["company"] == "Valeo"
    assert row["reason"] == "non_france" and row["count"] == 2
    assert json.loads(row["samples"]) == ["Berlin", "Munich"]
