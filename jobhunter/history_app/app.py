"""Streamlit entrypoint for the job-hunt history/funnel dashboard -- run via
`jobhunter history` (see jobhunter/cli.py). Only this file touches st.* calls;
data.py/charts.py hold the testable logic. Hardcodes the main FastAPI dashboard's
default port (8000) for the back-link -- if you run either app on a custom --port,
update the other side's hardcoded link too."""
from __future__ import annotations

import streamlit as st

from jobhunter import db
from jobhunter.history_app import charts, data

st.set_page_config(page_title="Job Hunt History", page_icon="📈", layout="wide")
st.link_button("← Back to Job Hunter dashboard", "http://127.0.0.1:8000")
st.title("📈 Job Hunt History")

conn = db.get_connection()

funnel_df = data.load_funnel(conn)
st.subheader("Funnel")
st.plotly_chart(charts.funnel_chart(funnel_df), width="stretch")

transitions_df = data.load_status_transitions(conn)
st.subheader("Status transitions")
if transitions_df.empty:
    st.caption("No status transitions recorded yet.")
else:
    nodes, links = data.build_sankey_nodes(transitions_df)
    st.plotly_chart(charts.sankey_chart(nodes, links), width="stretch")

st.subheader("Timeline")
timeline_df = data.load_timeline(conn, "status")
if timeline_df.empty:
    st.caption("No status events recorded yet.")
else:
    st.plotly_chart(charts.timeline_chart(timeline_df), width="stretch")

st.sidebar.header("Look up a job")
job_id = st.sidebar.number_input("Job ID", min_value=1, step=1, value=1)
if st.sidebar.button("Show history"):
    job_df = data.load_job_timeline(conn, int(job_id))
    if job_df.empty:
        st.sidebar.caption("No events for this job id.")
    else:
        st.dataframe(job_df, width="stretch")
