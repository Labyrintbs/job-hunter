"""Data-loading layer for the history dashboard -- plain functions, no st.* calls,
so they're unit-testable without running Streamlit. Each takes a sqlite3 connection
(or, for build_sankey_nodes, a plain DataFrame) and returns a pandas.DataFrame."""
from __future__ import annotations

import sqlite3

import pandas as pd

from jobhunter import db

FUNNEL_STAGES = ("new", "shortlisted", "cv_ready", "applied", "interview", "offer")


def load_funnel(conn: sqlite3.Connection) -> pd.DataFrame:
    """Jobs that *ever reached* each stage (via job_events), not just where they sit
    now -- an `applied` job counts toward `shortlisted` too, which plain
    applications.status can't show since it only holds current state."""
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    reach = {r["stage"]: r["n"] for r in db.stage_reach_counts(conn, FUNNEL_STAGES[1:])}
    rows = [{"stage": "new", "n": total}]
    rows += [{"stage": s, "n": reach.get(s, 0)} for s in FUNNEL_STAGES[1:]]
    return pd.DataFrame(rows)


def load_status_transitions(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = db.status_transition_counts(conn)
    return pd.DataFrame([dict(r) for r in rows], columns=["from_value", "to_value", "n"])


def build_sankey_nodes(transitions: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    """Pure transform, no DB access: maps a (from_value, to_value, n) frame to the
    node-label list + source/target index frame that Plotly's go.Sankey needs."""
    if transitions.empty:
        return [], pd.DataFrame(columns=["source", "target", "value"])
    labels = sorted(set(transitions["from_value"]) | set(transitions["to_value"]))
    index = {label: i for i, label in enumerate(labels)}
    links = pd.DataFrame({
        "source": transitions["from_value"].map(index),
        "target": transitions["to_value"].map(index),
        "value": transitions["n"],
    })
    return labels, links


def load_timeline(conn: sqlite3.Connection, event_type: str = "status") -> pd.DataFrame:
    """date, to_value, n, cumulative_n -- backs the timeline chart."""
    rows = db.events_by_day(conn, event_type)
    df = pd.DataFrame([dict(r) for r in rows], columns=["day", "to_value", "n"])
    if df.empty:
        return df.assign(cumulative_n=[])
    df = df.sort_values("day")
    df["cumulative_n"] = df.groupby("to_value")["n"].cumsum()
    return df


def load_job_timeline(conn: sqlite3.Connection, job_id: int) -> pd.DataFrame:
    rows = db.job_event_history(conn, job_id)
    return pd.DataFrame([dict(r) for r in rows],
                        columns=["occurred_at", "event_type", "from_value", "to_value",
                                 "detail", "source"])
