"""Pure Plotly figure-builders -- DataFrame in, go.Figure out, no st.* calls, so
these are unit-testable without running Streamlit."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


def funnel_chart(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Funnel(y=df["stage"], x=df["n"]))
    fig.update_layout(title="Jobs reaching each stage (ever)")
    return fig


def sankey_chart(nodes: list[str], links: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Sankey(
        node=dict(label=nodes, pad=15, thickness=16),
        link=dict(source=links["source"], target=links["target"], value=links["value"]),
    ))
    fig.update_layout(title="Status transition flow")
    return fig


def timeline_chart(df: pd.DataFrame) -> go.Figure:
    fig = px.line(df, x="day", y="cumulative_n", color="to_value",
                  title="Cumulative jobs reaching each status over time")
    return fig
