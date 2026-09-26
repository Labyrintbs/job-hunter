"""Company ATS boards — public JSON/XML, no auth.

Most "company career pages" are really a hosted ATS underneath, and several expose
a public board feed. Pulling those directly is how we reach postings that only live
on a company's own site (not on WTTJ/LinkedIn). Supported: Greenhouse, Lever, Ashby,
SmartRecruiters, Recruitee, Workable, Teamtailor, Personio, SuccessFactors, and
Workday (a different shape -- see sources/workday.py). All global, so we filter to
France at source.

Per-platform token format and quirks: see config/companies.yaml's header comment.
"""
from __future__ import annotations

import html
import re
import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from ..models import Job

_GOOGLE_NS = {"g": "http://base.google.com/ns/1.0"}

THROTTLE_SECONDS = 0.3
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

FRANCE_HINTS = (
    "france", "paris", "île-de-france", "ile-de-france", "lyon", "remote - europe",
    # Large-corporate ATS boards (esp. Workday) often report a city alone with no
    # country suffix -- these are the French sites that come up often enough in our
    # own tracked companies (Renault, Airbus, Valeo, ...) to be worth hardcoding.
    "toulouse", "bordeaux", "nantes", "lille", "marseille", "grenoble", "strasbourg",
    "rennes", "montpellier", "sophia antipolis", "guyancourt", "vélizy", "velizy",
    "boulogne-billancourt", "issy-les-moulineaux", "rueil-malmaison", "nanterre",
    "la défense", "la defense", "saint-ouen", "levallois-perret", "courbevoie",
    "massy", "saclay", "évry", "evry", "aubevoye", "lardy",
)


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


_SMARTRECRUITERS_PAGE_SIZE = 100
# Safety ceiling, not an expected real depth -- confirmed live that Veolia (2,941
# open postings) and Sopra Steria (2,014) both exceed one page under the old
# limit=100/no-pagination code, silently dropping >95% of their postings.
_SMARTRECRUITERS_MAX_PAGES = 50


def fetch_smartrecruiters(token: str, company: str, country_only: bool = True) -> list[Job]:
    jobs: list[Job] = []
    offset = 0
    with httpx.Client(timeout=20, headers=_UA) as c:
        for _ in range(_SMARTRECRUITERS_MAX_PAGES):
            url = (f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                   f"?limit={_SMARTRECRUITERS_PAGE_SIZE}&offset={offset}")
            resp = c.get(url)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            if not content:
                break
            for p in content:
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
            offset += _SMARTRECRUITERS_PAGE_SIZE
            if offset >= data.get("totalFound", 0):
                break
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


def fetch_teamtailor(token: str, company: str, country_only: bool = True) -> list[Job]:
    """One request per company -- confirmed live the feed has no `next_url`/cursor
    at all, so this is the whole board every time. If a response ever returns
    exactly 100 items, warn: a low-confidence report claims Teamtailor caps this
    feed at 100 with no way to page further, and the largest real company found
    during testing (71 jobs) wasn't big enough to confirm or rule that out."""
    url = f"https://{token}.teamtailor.com/jobs.json"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if len(items) == 100:
            print(f"  ats warn: {company} (teamtailor): got exactly 100 items -- "
                  f"possible undocumented cap, verify manually")
        for item in items:
            jp = item.get("_jobposting") or {}
            places = jp.get("jobLocation") or []
            location_parts = []
            countries = []
            for place in places:
                addr = (place or {}).get("address") or {}
                location_parts.append(", ".join(filter(None, [
                    addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry"),
                ])))
                if addr.get("addressCountry"):
                    countries.append(addr["addressCountry"])
            location = "; ".join(filter(None, location_parts))
            if country_only and "FR" not in countries and not _is_france(location):
                continue
            employer = (jp.get("hiringOrganization") or {}).get("name", "")
            jobs.append(Job(
                source="teamtailor",
                external_id=str(item.get("id", "")),
                title=(item.get("title") or "").strip(),
                company=company or employer,
                location=location,
                url=item.get("url", "") or "",
                description=_strip_html(jp.get("description", "") or "")[:5000],
                posted_at=jp.get("datePosted", "") or item.get("date_published", "") or "",
            ))
    return jobs


def fetch_personio(token: str, company: str, country_only: bool = True) -> list[Job]:
    """The XML feed is an opt-in setting each Personio customer enables themselves
    -- a 404 means "not turned on for this company," not a fetch failure, so it's
    swallowed quietly rather than raised (confirmed live: 4 of 5 real companies
    tried had it on, 1 didn't). The feed has no apply-URL field, so one is built
    from the known job page pattern instead."""
    url = f"https://{token}.jobs.personio.com/xml?language=en"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
        for pos in root.findall("position"):
            offices = [pos.findtext("office", "") or ""]
            offices += [o.text or "" for o in pos.findall("additionalOffices/office")]
            location = ", ".join(filter(None, offices))
            if country_only and not _is_france(location):
                continue
            desc = " ".join((d.findtext("value") or "") for d in pos.findall("jobDescriptions/jobDescription"))
            pid = pos.findtext("id", "") or ""
            jobs.append(Job(
                source="personio",
                external_id=pid,
                title=(pos.findtext("name") or "").strip(),
                company=company,
                location=location,
                url=f"https://{token}.jobs.personio.com/job/{pid}",
                description=_strip_html(desc)[:5000],
                contract_type=pos.findtext("employmentType", "") or "",
                posted_at=pos.findtext("createdAt", "") or "",
            ))
    return jobs


def fetch_successfactors(token: str, company: str, country_only: bool = True) -> list[Job]:
    """`token` format: see config/companies.yaml's header. RSS 2.0 with the Google
    Merchant namespace; confirmed live most tenants omit `pubDate` (carrying only
    g:expiration_date instead), so posted_at is left blank rather than guessed
    from that -- would be misleading."""
    url = f"https://{token}/sitemal.xml"
    jobs: list[Job] = []
    with httpx.Client(timeout=20, headers=_UA) as c:
        resp = c.get(url)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            location = item.findtext("g:location", namespaces=_GOOGLE_NS) or title
            if country_only and not _is_france(location):
                continue
            gid = item.findtext("g:id", namespaces=_GOOGLE_NS)
            guid = item.findtext("guid")
            employer = item.findtext("g:employer", namespaces=_GOOGLE_NS) or company
            posted_at = item.findtext("pubDate") or ""
            if posted_at:
                try:
                    posted_at = parsedate_to_datetime(posted_at).isoformat()
                except (TypeError, ValueError):
                    posted_at = ""
            jobs.append(Job(
                source="successfactors",
                external_id=(gid or guid or item.findtext("link") or "").strip(),
                title=title,
                company=employer,
                location=item.findtext("g:location", namespaces=_GOOGLE_NS) or "",
                url=item.findtext("link", "") or "",
                description=_strip_html(item.findtext("description", "") or "")[:5000],
                posted_at=posted_at,
            ))
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workable": fetch_workable,
    "teamtailor": fetch_teamtailor,
    "personio": fetch_personio,
    "successfactors": fetch_successfactors,
}
SUPPORTED_ATS = tuple(FETCHERS)


def fetch_all(companies: list[dict], workday_queries: list[str] | None = None) -> list[Job]:
    """companies: list of {name, ats, token} where ats is one of SUPPORTED_ATS, or for
    ats='workday' a {name, ats, tenant, wd_host, site, locale?} entry instead (Workday
    has no single global token -- see sources/workday.py). workday_queries is passed
    straight through to workday.fetch (falls back to its own DEFAULT_QUERIES if not
    given here)."""
    from . import workday  # local import: workday.py imports helpers from this module

    out: list[Job] = []
    for co in companies:
        ats = (co.get("ats") or "").lower()
        token = co.get("token", "")
        name = co.get("name", token)
        try:
            if ats == "workday":
                out.extend(workday.fetch(co["tenant"], co["wd_host"], co["site"], name,
                                         locale=co.get("locale", "en-US"),
                                         queries=workday_queries))
            else:
                fetcher = FETCHERS.get(ats)
                if not fetcher:
                    print(f"  ats warn: {name}: unknown ats '{ats}'")
                    continue
                out.extend(fetcher(token, name))
        except Exception as exc:  # one bad board must not sink the run
            print(f"  ats warn: {name} ({ats}) failed: {exc}")
        time.sleep(THROTTLE_SECONDS)
    return out
