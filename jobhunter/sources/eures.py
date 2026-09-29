"""EURES (official EU job mobility portal) -- public JSON API, no auth.

Reverse-engineered (no official docs), but confirmed live: no API key, no
referer check, robots.txt doesn't mention /eures -- same risk profile as this
repo's WTTJ integration. Its terms reportedly restrict programmatic use to
recognised EURES partners; nothing technically enforces that.

`locationCodes: ["fr"]` is an exact server-side country filter (like WTTJ's
Algolia facet), not a text heuristic. Confirmed live NOT recency-sorted (a
July posting appeared in a September top 20) -- neither this fetcher nor the
deeper weekly backfill (see pipeline.backfill_eures) can guarantee catching
every new posting; no sort-override param exists.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from .. import fetch_diag
from ..models import Job
from .ats import _UA, _strip_html

SEARCH_URL = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
DETAIL_URL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=fr"
THROTTLE_SECONDS = 0.3
PAGE_SIZE = 50   # the API rejects resultsPerPage > 50 (HTTP 400)

# Eurostat NUTS3 codes for the 8 Île-de-France departments, plus this project's
# curated major French tech hubs (config/search.yaml's major_cities) -- match.
# geo_tier() only recognizes these via place names, not NUTS codes, so map known
# codes to the name it actually looks for. City codes verified live against
# EURES's own search results (the NUTS3 code shared by postings titled after
# that city). Any other code (most of France) falls back to plain "France" --
# a department can contain several cities, so an unmapped code isn't guessed at.
_KNOWN_NUTS = {
    "FR101": "Paris, Île-de-France",
    "FR102": "Île-de-France",
    "FR103": "Île-de-France",
    "FR104": "Île-de-France",
    "FR105": "Île-de-France",
    "FR106": "Île-de-France",
    "FR107": "Île-de-France",
    "FR108": "Île-de-France",
    "FRK26": "Lyon",
    "FRJ23": "Toulouse",
    "FRK24": "Grenoble",
    "FRL03": "Nice",
    "FRE11": "Lille",
    "FRG01": "Nantes",
    "FRI12": "Bordeaux",
    "FRH03": "Rennes",
    "FRJ13": "Montpellier",
    "FRF11": "Strasbourg",
}


def _location(location_map: dict) -> str:
    codes = location_map.get("FR") or []
    if not codes:
        return "France"
    parts: list[str] = []
    for code in codes:
        name = _KNOWN_NUTS.get(code, "France")
        if name not in parts:
            parts.append(name)
    return "; ".join(parts)


def _posted_at(creation_date_ms) -> str:
    try:
        return datetime.fromtimestamp(int(creation_date_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def fetch(query: str, max_hits: int = 200, country: str = "fr") -> list[Job]:
    jobs: list[Job] = []
    page = 1
    total = 0
    with httpx.Client(timeout=30, headers=_UA) as c:
        while len(jobs) < max_hits:
            body = {
                "keywords": [{"keyword": query, "specificSearchCode": "EVERYWHERE"}],
                "locationCodes": [country],
                "resultsPerPage": PAGE_SIZE,
                "page": page,
            }
            resp = c.post(SEARCH_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("numberRecords", 0)
            jvs = data.get("jvs", [])
            if not jvs:
                break
            for jv in jvs:
                jid = jv.get("id", "")
                jobs.append(Job(
                    source="eures",
                    external_id=str(jid),
                    title=(jv.get("title") or "").strip(),
                    company=(jv.get("employer") or {}).get("name", "") or "",
                    location=_location(jv.get("locationMap") or {}),
                    url=DETAIL_URL.format(id=jid),
                    description=_strip_html(jv.get("description", "") or "")[:5000],
                    posted_at=_posted_at(jv.get("creationDate")),
                ))
            page += 1
            time.sleep(THROTTLE_SECONDS)
            if len(jvs) < PAGE_SIZE:
                break
    if len(jobs) >= max_hits and (page - 1) * PAGE_SIZE < total:
        fetch_diag.track("eures", "pagination_cap_hit", detail=f"{query!r}: total={total} max_hits={max_hits}")
    return jobs[:max_hits]
