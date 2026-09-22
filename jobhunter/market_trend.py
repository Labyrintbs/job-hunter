"""Daily snapshot of France Travail's official IT/CS-market open-postings counts.

Entirely independent of the job-search pipeline (jobhunter/pipeline.py,
config["francetravail"]["queries"]) -- this tracks overall market demand as a
standalone time series, not candidates to apply to. Categories are France
Travail's own domaine/ROME codes (see /referentiel/domaines,
/referentiel/metiers), not free-text keyword guesses.
"""
from __future__ import annotations

from . import db
from .config import load_search_config
from .sources import francetravail

# domaine=M18 is France Travail's own aggregate for the whole "Systemes
# d'information et de telecommunication" domain; the codeROME entries are a
# representative slice of it (verified live, real counts at design time:
# M1805=383, M1811=490, M1889=215, M1827=318, M1856=227) -- not the personal
# job-search query list, which stays in config["francetravail"]["queries"].
DEFAULT_CATEGORIES = [
    {"label": "Whole IT/CS market", "param": "domaine", "code": "M18"},
    {"label": "Software development", "param": "codeROME", "code": "M1805"},
    {"label": "Data engineering", "param": "codeROME", "code": "M1811"},
    {"label": "AI engineering", "param": "codeROME", "code": "M1889"},
    {"label": "DevOps", "param": "codeROME", "code": "M1827"},
    {"label": "Cybersecurity", "param": "codeROME", "code": "M1856"},
]


def snapshot(config: dict | None = None) -> dict:
    """Record today's market_snapshots rows: official France Travail
    open-posting counts per configured IT/CS category, at France-wide and
    Île-de-France scope. Returns {"recorded": n, "failed": [...]}."""
    config = config or load_search_config()
    mt_cfg = config.get("market_trend") or {}
    if not mt_cfg.get("enabled", True):
        return {"recorded": 0, "failed": []}

    categories = mt_cfg.get("categories") or DEFAULT_CATEGORIES
    idf = mt_cfg.get("idf_departements", francetravail.IDF_DEPARTEMENTS)

    recorded = 0
    failed: list[str] = []
    db.init_db()
    with db.connect() as conn:
        for cat in categories:
            filters = {cat["param"]: cat["code"]}
            for scope, departements in (("france", None), ("idf", idf)):
                total = francetravail.count(departements=departements, **filters)
                if total is None:
                    failed.append(f"{cat['label']} ({scope})")
                    continue
                db.record_market_snapshot(conn, scope, cat["label"], total)
                recorded += 1
    return {"recorded": recorded, "failed": failed}
