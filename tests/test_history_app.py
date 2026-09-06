import pandas as pd

from jobhunter import db
from jobhunter.history_app import charts, data
from jobhunter.models import Job


def J(ext_id, title="ML Engineer", url="", company="Acme", loc="Paris"):
    return Job(source="wttj", external_id=ext_id, title=title, company=company,
               location=loc, url=url)


def test_load_funnel_returns_expected_stages_and_counts(tmp_db):
    with db.connect() as conn:
        progressed, _ = db.upsert_job(conn, J("1", url="http://x/1", company="CoA"), 60, "r")
        db.update_status(conn, progressed, "shortlisted")
        db.update_status(conn, progressed, "cv_ready")
        db.update_status(conn, progressed, "applied")
        db.upsert_job(conn, J("2", url="http://x/2", company="CoB"), 60, "r")  # stays at 'new'

        df = data.load_funnel(conn)
        counts = dict(zip(df["stage"], df["n"]))
        assert list(df["stage"]) == list(data.FUNNEL_STAGES)
        assert counts["new"] == 2
        assert counts["shortlisted"] == 1
        assert counts["cv_ready"] == 1
        assert counts["applied"] == 1
        assert counts["interview"] == 0
        assert counts["offer"] == 0


def test_load_status_transitions_builds_expected_frame(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "shortlisted")
        df = data.load_status_transitions(conn)
        assert list(df.columns) == ["from_value", "to_value", "n"]
        row = df[(df["from_value"] == "new") & (df["to_value"] == "shortlisted")].iloc[0]
        assert row["n"] == 1


def test_build_sankey_nodes_maps_labels_to_indices():
    transitions = pd.DataFrame([
        {"from_value": "new", "to_value": "shortlisted", "n": 3},
        {"from_value": "shortlisted", "to_value": "cv_ready", "n": 2},
    ])
    nodes, links = data.build_sankey_nodes(transitions)
    assert nodes == ["cv_ready", "new", "shortlisted"]
    idx = {n: i for i, n in enumerate(nodes)}
    assert links.iloc[0]["source"] == idx["new"]
    assert links.iloc[0]["target"] == idx["shortlisted"]
    assert links.iloc[0]["value"] == 3


def test_build_sankey_nodes_handles_empty_frame():
    nodes, links = data.build_sankey_nodes(pd.DataFrame(columns=["from_value", "to_value", "n"]))
    assert nodes == []
    assert links.empty


def test_load_timeline_cumulative_counts(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "shortlisted")
        # backdate events to two distinct days to exercise cumulative summing
        conn.execute("UPDATE job_events SET occurred_at = '2026-01-01 00:00:00' "
                     "WHERE job_id = ? AND event_type = 'created'", (jid,))
        conn.execute("UPDATE job_events SET occurred_at = '2026-01-02 00:00:00' "
                     "WHERE job_id = ? AND event_type = 'status'", (jid,))
        jid2, _ = db.upsert_job(conn, J("2", url="http://x/2", company="CoB"), 60, "r")
        db.update_status(conn, jid2, "shortlisted")
        conn.execute("UPDATE job_events SET occurred_at = '2026-01-03 00:00:00' "
                     "WHERE job_id = ? AND event_type = 'status'", (jid2,))

        df = data.load_timeline(conn, "status")
        shortlisted = df[df["to_value"] == "shortlisted"].sort_values("day")
        assert list(shortlisted["cumulative_n"]) == [1, 2]


def test_load_job_timeline_returns_full_history_for_one_job(tmp_db):
    with db.connect() as conn:
        jid, _ = db.upsert_job(conn, J("1", url="http://x/1"), 60, "r")
        db.update_status(conn, jid, "shortlisted")
        df = data.load_job_timeline(conn, jid)
        assert list(df["event_type"]) == ["created", "status"]


def test_funnel_chart_returns_figure_with_funnel_trace():
    df = pd.DataFrame({"stage": ["new", "applied"], "n": [10, 3]})
    fig = charts.funnel_chart(df)
    assert fig.data[0].type == "funnel"


def test_sankey_chart_returns_figure_with_sankey_trace():
    nodes, links = data.build_sankey_nodes(pd.DataFrame([
        {"from_value": "new", "to_value": "shortlisted", "n": 3},
    ]))
    fig = charts.sankey_chart(nodes, links)
    assert fig.data[0].type == "sankey"


def test_timeline_chart_returns_figure_with_expected_series_count():
    df = pd.DataFrame({
        "day": ["2026-01-01", "2026-01-02", "2026-01-01"],
        "to_value": ["shortlisted", "shortlisted", "applied"],
        "n": [1, 1, 1],
        "cumulative_n": [1, 2, 1],
    })
    fig = charts.timeline_chart(df)
    assert len(fig.data) == 2   # one line per distinct to_value
