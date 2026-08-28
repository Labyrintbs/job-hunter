"""Company ATS boards — public JSON, no auth.

Most "company career pages" are really a hosted ATS underneath, and several expose
a public board API. Pulling those directly is how we reach postings that only live
on a company's own site (not on WTTJ/LinkedIn). Supported: Greenhouse, Lever, Ashby,
SmartRecruiters, Recruitee, Workable. All global, so we filter to France at source.
(Teamtailor/Workday are intentionally omitted — their APIs require a per-tenant token.)
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


def fetch_ashby(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            location = j.get("location", "") or ""
            country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")) or ""
            if country_only and not _is_france(location, country):
                continue
            jobs.append(Job(
                source="ashby",
                external_id=str(j.get("id")),
                title=(j.get("title") or "").strip(),
                company=company,
                location=location,
                url=j.get("jobUrl") or j.get("applyUrl", "") or "",
                description=_strip_html(j.get("descriptionHtml", "") or "")[:5000],
                contract_type=j.get("employmentType", "") or "",
                posted_at=j.get("publishedAt", "") or "",
            ))
    return jobs


def fetch_smartrecruiters(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for p in resp.json().get("content", []):
            loc = p.get("location") or {}
            location = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            if country_only and not _is_france(location, loc.get("country", "")):
                continue
            pid = p.get("id")
            jobs.append(Job(
                source="smartrecruiters",
                external_id=str(pid),
                title=(p.get("name") or "").strip(),
                company=company or (p.get("company") or {}).get("name", ""),
                location=location + (" (remote)" if loc.get("remote") else ""),
                url=f"https://jobs.smartrecruiters.com/{token}/{pid}",
                posted_at=p.get("releasedDate", "") or "",
            ))
    return jobs


def fetch_recruitee(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://{token}.recruitee.com/api/offers/"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for o in resp.json().get("offers", []):
            location = ", ".join(filter(None, [o.get("city"), o.get("country")])) or o.get("location", "")
            if country_only and not _is_france(location, o.get("country_code", "")):
                continue
            jobs.append(Job(
                source="recruitee",
                external_id=str(o.get("id")),
                title=(o.get("title") or "").strip(),
                company=company,
                location=location,
                url=o.get("careers_url") or o.get("url", "") or "",
                description=_strip_html(o.get("description", "") or "")[:5000],
                contract_type=o.get("employment_type_code", "") or "",
                posted_at=o.get("published_at", "") or "",
            ))
    return jobs


def fetch_workable(token: str, company: str, country_only: bool = True) -> list[Job]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            location = ", ".join(filter(None, [j.get("city"), j.get("country")])) or j.get("location", "")
            if country_only and not _is_france(location, j.get("country", "")):
                continue
            jobs.append(Job(
                source="workable",
                external_id=str(j.get("shortcode") or j.get("id")),
                title=(j.get("title") or "").strip(),
                company=company or j.get("company", ""),
                location=location,
                url=j.get("url") or j.get("application_url", "") or "",
                description=_strip_html(j.get("description", "") or "")[:5000],
                contract_type=j.get("employment_type", "") or "",
                posted_at=j.get("published_on", "") or "",
            ))
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workable": fetch_workable,
}
SUPPORTED_ATS = tuple(FETCHERS)


def fetch_all(companies: list[dict]) -> list[Job]:
    """companies: list of {name, ats, token} where ats is one of SUPPORTED_ATS."""
    out: list[Job] = []
    for co in companies:
        ats = (co.get("ats") or "").lower()
        token = co.get("token", "")
        name = co.get("name", token)
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            print(f"  ats warn: {name}: unknown ats '{ats}'")
            continue
        try:
            out.extend(fetcher(token, name))
        except Exception as exc:  # one bad board must not sink the run
            print(f"  ats warn: {name} ({ats}) failed: {exc}")
        time.sleep(THROTTLE_SECONDS)
    return out
