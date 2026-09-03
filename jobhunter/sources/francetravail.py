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
import time

import httpx

from ..models import Job

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

# Île-de-France departments.
IDF_DEPARTEMENTS = "75,92,93,94,77,78,91,95"

PAGE_SIZE = 50
THROTTLE_SECONDS = 0.3

_token_cache: dict[str, tuple[str, float]] = {}


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


def fetch(query: str, departements: str = IDF_DEPARTEMENTS,
          max_results: int = 150) -> list[Job]:
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        return []  # not registered yet -- silently contribute nothing

    token = _get_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}
    jobs: list[Job] = []
    start = 0
    with httpx.Client(timeout=20, headers=headers) as client:
        while len(jobs) < max_results:
            end = start + PAGE_SIZE - 1
            resp = client.get(SEARCH_URL, params={
                "motsCles": query,
                "departement": departements,
            }, headers={"Range": f"offres {start}-{end}"})
            if resp.status_code not in (200, 206):
                break
            data = resp.json()
            results = data.get("resultats", [])
            if not results:
                break
            jobs.extend(_to_job(o) for o in results)
            start += PAGE_SIZE
            time.sleep(THROTTLE_SECONDS)
            if len(results) < PAGE_SIZE:
                break
    return jobs[:max_results]
