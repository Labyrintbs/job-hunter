from __future__ import annotations

from . import db, match
from .config import load_search_config
from .sources import wttj


def run_fetch(config: dict | None = None) -> dict:
    config = config or load_search_config()
    db.init_db()

    jobs = wttj.fetch(
        query=config["query"],
        max_hits=config.get("max_hits", 100),
        country=(config.get("countries") or ["France"])[0],
    )

    seen = 0
    kept = 0
    new = 0
    with db.connect() as conn:
        for job in jobs:
            seen += 1
            pts, reasons, keep = match.evaluate(job, config)
            if not keep:
                continue
            kept += 1
            _, is_new = db.upsert_job(conn, job, pts, reasons)
            if is_new:
                new += 1

    return {"fetched": seen, "kept": kept, "new": new}
