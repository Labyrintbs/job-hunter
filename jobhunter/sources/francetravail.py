"""France Travail (formerly Pôle emploi) — official public "Offres d'emploi" API.

Unlike every other source here, this is a documented, authenticated government API
(https://francetravail.io) rather than a scrape — no ToS risk, no bot-detection
concerns. It needs a free client_id/client_secret from a francetravail.io account
(see README), read from FRANCE_TRAVAIL_CLIENT_ID / FRANCE_TRAVAIL_CLIENT_SECRET.
Without those set, fetch() returns [] so the rest of the pipeline is unaffected --
same posture as linkedin.enabled=false.

Offers include the full description inline, so unlike LinkedIn/most ATS boards,
these never need a separate enrichment pass.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from .. import fetch_diag
from ..models import Job

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

# Île-de-France departments.
IDF_DEPARTEMENTS = "75,92,93,94,77,78,91,95"

PAGE_SIZE = 50
THROTTLE_SECONDS = 0.3
# The API rejects a "departement" filter with more than 5 values (HTTP 400: "Le
# nombre de departements maximum autorise pour la recherche est de 5") -- verified
# live. IDF alone is already 8, so any real departements list needs batching.
MAX_DEPARTEMENTS_PER_REQUEST = 5

_token_cache: dict[str, tuple[str, float]] = {}
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/(\d+)$")


def _dept_batches(departements: str | None) -> list[str | None]:
    """Split a comma-separated departement string into groups of at most
    MAX_DEPARTEMENTS_PER_REQUEST -- the API rejects more than that in one
    request. None (no filter, nationwide) stays a single [None] batch."""
    if not departements:
        return [None]
    dept_list = [d.strip() for d in departements.split(",") if d.strip()]
    return [
        ",".join(dept_list[i:i + MAX_DEPARTEMENTS_PER_REQUEST])
        for i in range(0, len(dept_list), MAX_DEPARTEMENTS_PER_REQUEST)
    ] or [None]


def _get_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.time():
        return cached[0]
    resp = httpx.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": SCOPE,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    _token_cache[client_id] = (token, time.time() + data.get("expires_in", 1499) - 30)
    return token


def _location(offer: dict) -> str:
    lieu = offer.get("lieuTravail") or {}
    return lieu.get("libelle", "") or ""


def _to_job(offer: dict) -> Job:
    entreprise = offer.get("entreprise") or {}
    origine = offer.get("origineOffre") or {}
    return Job(
        source="francetravail",
        external_id=str(offer.get("id", "")),
        title=(offer.get("intitule") or "").strip(),
        company=entreprise.get("nom", "") or "",
        location=_location(offer),
        url=origine.get("urlOrigine", "") or "",
        description=(offer.get("description") or "")[:5000],
        contract_type=offer.get("typeContrat", "") or "",
        posted_at=offer.get("dateCreation", "") or "",
    )


def _search(query: str, departements: str | None, max_results: int,
            extra_params: dict | None = None) -> list[Job]:
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        fetch_diag.track("francetravail", "credentials_missing", detail=query)
        return []  # not registered yet -- silently contribute nothing

    token = _get_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    jobs: list[Job] = []
    seen_ids: set[str] = set()
    with httpx.Client(timeout=20, headers=headers) as client:
        for batch in _dept_batches(departements):
            start = 0
            while len(jobs) < max_results:
                end = start + PAGE_SIZE - 1
                params = {"motsCles": query, **(extra_params or {})}
                if batch:
                    params["departement"] = batch
                resp = client.get(SEARCH_URL, params=params,
                                   headers={"Range": f"offres {start}-{end}"})
                if resp.status_code not in (200, 206):
                    break
                data = resp.json()
                results = data.get("resultats", [])
                if not results:
                    break
                for o in results:
                    oid = str(o.get("id", ""))
                    if oid not in seen_ids:
                        seen_ids.add(oid)
                        jobs.append(_to_job(o))
                if len(jobs) >= max_results and len(results) == PAGE_SIZE:
                    fetch_diag.track("francetravail", "pagination_cap_hit",
                                      detail=f"{query!r} batch={batch} max_results={max_results}")
                start += PAGE_SIZE
                time.sleep(THROTTLE_SECONDS)
                if len(results) < PAGE_SIZE:
                    break
            if len(jobs) >= max_results:
                break
    return jobs[:max_results]


def fetch(query: str, departements: str = IDF_DEPARTEMENTS,
          max_results: int = 150) -> list[Job]:
    return _search(query, departements, max_results)


def fetch_before(query: str, departements: str, before_date: str | None,
                  window_days: int = 90, max_results: int = 150) -> tuple[list[Job], str, bool]:
    """Backfill entry point (see pipeline.backfill_francetravail): searches a
    fixed calendar window [before_date - window_days, before_date), using the
    API's own documented minCreationDate/maxCreationDate params (ISO-8601).
    Unlike arbeitnow/wttj/workday's page/offset position, a date window means
    the same real postings every time regardless of how many newer ones exist
    -- safe to resume from a saved date, not just a one-shot deep walk.
    before_date=None starts at now. Returns (jobs, next_before_date, reached_end)
    -- next_before_date is the window's own start, to walk further back next
    call; reached_end is True once a window comes back empty (assume no
    listings remain that old)."""
    end = (datetime.now(timezone.utc) if not before_date
           else datetime.fromisoformat(before_date))
    start = end - timedelta(days=window_days)
    extra = {
        "minCreationDate": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxCreationDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    jobs = _search(query, departements, max_results, extra_params=extra)
    return jobs, start.strftime("%Y-%m-%dT%H:%M:%SZ"), len(jobs) == 0


def count(departements: str | None = None, **filters: str) -> int | None:
    """Exact total open-postings count for arbitrary search filters (e.g.
    domaine="M18", codeROME="M1805" -- France Travail's own official IT/CS
    taxonomy, not a free-text guess), via the Content-Range header of a
    1-row request -- no job details fetched. None on missing credentials or
    any batch failure, never a silently-wrong partial number (a posting's
    departement is a single value, so batches partition cleanly and their
    totals are safe to sum)."""
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None

    token = _get_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    total = 0
    with httpx.Client(timeout=20, headers=headers) as client:
        for batch in _dept_batches(departements):
            params = dict(filters)
            if batch:
                params["departement"] = batch
            resp = client.get(SEARCH_URL, params=params, headers={"Range": "offres 0-0"})
            if resp.status_code not in (200, 206):
                return None
            m = _CONTENT_RANGE_TOTAL_RE.search(resp.headers.get("Content-Range", ""))
            if not m:
                return None
            total += int(m.group(1))
    return total
