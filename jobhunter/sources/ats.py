"""Company ATS boards (Greenhouse + Lever) — public JSON, no auth.

Many Paris tech companies host their jobs on Greenhouse or Lever, whose board
APIs are public and ToS-friendlier than scraping aggregators. These boards are
global, so we filter to France at the source.
"""
from __future__ import annotations

import html
import re
import time

import httpx

from ..models import Job

THROTTLE_SECONDS = 0.3
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

FRANCE_HINTS = ("france", "paris", "île-de-france", "ile-de-france", "lyon", "remote - europe")


def _is_france(location: str, country: str = "") -> bool:
    location = location or ""
    country = country or ""
    if country.strip().upper() == "FR":
        return True
    return any(h in f"{location} {country}".lower() for h in FRANCE_HINTS)


def _strip_html(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()


def fetch_greenhouse(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            location = (j.get("location") or {}).get("name", "")
            if country_only and not _is_france(location):
                continue
            jobs.append(Job(
                source="greenhouse",
                external_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                company=company or j.get("company_name", ""),
                location=location,
                language=j.get("language", "") or "",
                url=j.get("absolute_url", ""),
                description=_strip_html(j.get("content", ""))[:5000],
                posted_at=j.get("updated_at", "") or "",
            ))
    return jobs


def fetch_lever(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for p in resp.json():
            cats = p.get("categories") or {}
            location = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
            if country_only and not _is_france(location, p.get("country", "")):
                continue
            desc = " ".join(filter(None, [p.get("descriptionPlain"), p.get("additionalPlain")]))
            jobs.append(Job(
                source="lever",
                external_id=str(p.get("id")),
                title=(p.get("text") or "").strip(),
                company=company,
                location=location,
                url=p.get("hostedUrl", ""),
                description=desc[:5000],
                contract_type=cats.get("commitment", "") or "",
            ))
    return jobs


def fetch_all(companies: list[dict]) -> list[Job]:
    """companies: list of {name, ats: greenhouse|lever, token}."""
    out: list[Job] = []
    for co in companies:
        ats = (co.get("ats") or "").lower()
        token = co.get("token", "")
        name = co.get("name", token)
        try:
            if ats == "greenhouse":
                out.extend(fetch_greenhouse(token, name))
            elif ats == "lever":
                out.extend(fetch_lever(token, name))
        except Exception as exc:  # one bad board must not sink the run
            print(f"  ats warn: {name} ({ats}) failed: {exc}")
        time.sleep(THROTTLE_SECONDS)
    return out
