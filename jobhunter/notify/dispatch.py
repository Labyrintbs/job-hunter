"""Dispatch a digest of new jobs to the configured, available channels."""
from __future__ import annotations

from . import channels, digest


def send(rows: list[dict], config: dict) -> dict:
    """Notify about new jobs. Returns {selected, results: {channel: status}}."""
    notif = config.get("notifications") or {}
    if not notif.get("enabled", True):
        return {"selected": 0, "results": {}, "skipped": "disabled"}

    selected = digest.select(rows, notif.get("min_score", 60))
    if not selected:
        return {"selected": 0, "results": {}}

    subject = digest.subject(selected)
    text = digest.text(selected)
    md = digest.markdown(selected)

    wanted = notif.get("channels") or ["file"]
    results: dict[str, str] = {}
    for name in wanted:
        ch = channels.ALL_CHANNELS.get(name)
        if not ch:
            results[name] = "unknown channel"
        elif not ch.available():
            results[name] = "not configured"
        else:
            try:
                results[name] = ch.send(subject, text, md)
            except Exception as exc:
                results[name] = f"error: {exc}"
    return {"selected": len(selected), "results": results}
