"""Rule-based screening for a Paris junior ML-engineer hunt.

Two-stage triage:
  * keep     — store it at all (ML-relevant by title, not a hard-excluded intern/blocklist).
  * filtered — stored but auto-hidden into the Filtered bucket (senior title, too many
               years required, or below min_score). Never deleted, so false-negatives
               stay reviewable and can be rescued.

Seniority is a hard bucket gate here (not just a soft penalty) because obvious
senior/lead/staff titles are the main noise for a junior search — but a junior/new-grad
title always wins, so a "5 years" mention in a junior posting can't push it out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Job

# Senior markers (FR+EN). Title hit => senior bucket; description-only hit => ranking penalty.
SENIOR_TERMS = [
    "senior", "sr.", "lead", "principal", "staff", "head of", "head of ai",
    "confirmé", "confirme", "expert", "director", "directeur", "vp ", "vice president",
    "expérimenté", "experimente", "manager",
]
# Junior markers always protect a posting from the seniority gate.
JUNIOR_TERMS = [
    "junior", "new grad", "new-grad", "graduate", "entry level", "entry-level",
    "débutant", "debutant", "jeune diplômé", "jeune diplome", "associate",
]

# Explicit citizenship-as-eligibility clauses (distinct from generic "security clearance"
# language, which is a softer signal). Hard-gates the posting regardless of everything
# else -- see templates/cv_tailoring_workflow.md's "Citizenship / eligibility" rule.
_CITIZENSHIP_RE = re.compile(
    r"must (?:hold|have)[a-z\s]{0,25}citizenship"
    r"|citizenship (?:in|of) the target territory"
    r"|citizenship requirement"
    r"|(?:french|eu|european|us|u\.s\.) citizenship (?:is )?required"
    r"|require[sd]? .{0,20}citizenship"
    r"|nationalité (?:française )?(?:est )?(?:requise|exigée)"
    r"|être de nationalité (?:française|europ[ée]enne)",
    re.IGNORECASE,
)

# High-precision phrases only (a bare "mission"/"conseil" is too common in unrelated
# boilerplate) -- soft signal (score penalty), not a hard filter, since some
# consultancies have genuinely strong technical roles.
CLIENT_FACING_TERMS = [
    "pre-sales", "presales", "pre sales", "avant-vente",
    "forward deployed", "forward-deployed",
    "customer-facing", "customer facing",
    "cabinet de conseil", "conseil et ingénierie",
    "chez nos clients", "chez le client", "accompagner nos clients",
    "pour le compte d'un client", "pour nos clients",
    "contexte de la mission", "en mission chez",
]


def has_citizenship_requirement(text: str) -> bool:
    return bool(_CITIZENSHIP_RE.search(text))

# Only counts when tied to an experience context (so "founded 8 years ago" isn't
# read as "requires 8 years"). Also accepts work verbs beyond "experience" itself
# so "4+ years working on large-scale ML codebases" parses too.
_EXP_YEARS_RE = re.compile(
    r"(?:(\d{1,2})\s*\+?\s*(?:-|–|to|à|au)?\s*\d{0,2}\s*"
    r"(?:years?|yrs?|ans|années?|annees?)[^.\n]{0,40}?"
    r"(?:experience|expérience|experiences?|exp\b|d['’]exp"
    r"|working|building|developing|leading|managing|shipping|delivering"
    r"|travaillant|travaillé|construisant)"
    r"|(?:experience|expérience|minimum|at least|au moins)[^.\n]{0,40}?"
    r"(\d{1,2})\s*\+?\s*(?:-|–|to|à)?\s*\d{0,2}\s*(?:years?|yrs?|ans|années?|annees?))",
    re.IGNORECASE,
)


def _text(job: Job) -> str:
    return f"{job.title} {job.contract_type} {job.description}".lower()


def min_years_required(text: str) -> int | None:
    """Lowest experience-in-years floor found in the text, or None. Conservative: takes
    the minimum across mentions and ignores absurd values (>15, likely company age)."""
    nums: list[int] = []
    for m in _EXP_YEARS_RE.finditer(text):
        n = m.group(1) or m.group(2)
        if n:
            nums.append(int(n))
    nums = [n for n in nums if 0 < n <= 15]
    return min(nums) if nums else None


def detect_seniority(job: Job) -> str:
    """junior | senior | unknown. Title junior markers win outright."""
    title = job.title.lower()
    if any(t in title for t in JUNIOR_TERMS):
        return "junior"
    if any(t in title for t in SENIOR_TERMS):
        return "senior"
    return "unknown"


def is_excluded(job: Job, config: dict) -> str | None:
    text = f"{job.title} {job.contract_type}".lower()
    for term in config.get("exclude_terms", []):
        if term.lower() in text:
            return f"excluded: contains '{term}'"
    company = job.company.lower()
    for blocked in config.get("company_blocklist", []):
        if blocked.lower() == company:
            return f"excluded: blocked company '{job.company}'"
    return None


_REMOTE_TERMS = ("remote", "télétravail", "teletravail", "full remote", "100% remote",
                  "work from home", "home office", "wfh")


def detect_remote_from_text(text: str) -> bool:
    """Keyword check for a genuine remote mention in a job's own full text (title +
    description). Used to confirm remote status from real JD content once enrichment
    lands, for sources (e.g. LinkedIn) whose search-side "remote" filter was verified
    unreliable on its own -- see pipeline.enrich_one. Same simple substring approach
    as geo_tier()'s location check, and the same technique established scrapers (e.g.
    JobSpy) use for the same reason: no site gives a fully trustworthy structured
    signal here except a facet independently confirmed against the posting itself."""
    t = (text or "").lower()
    return any(term in t for term in _REMOTE_TERMS)


def geo_tier(location: str, config: dict) -> str:
    """Coarse geography bucket for analytics: idf | major_city | europe_remote |
    remote | france | outside | unknown. IDF wins first (it's the application
    focus), then the named major-city list (a bare city name like "Lyon" has no
    "France" in it, so it needs its own check ahead of the generic literal-"france"
    fallback), then remote (split into europe_remote vs plain remote depending on
    whether an EU country name is present alongside the remote term)."""
    loc = (location or "").lower()
    if not loc:
        return "unknown"
    if any(l.lower() in loc for l in config.get("locations", [])):
        return "idf"
    if any(c.lower() in loc for c in config.get("major_cities", [])):
        return "major_city"
    if any(t in loc for t in _REMOTE_TERMS):
        if any(c.lower() in loc for c in config.get("europe_countries", [])):
            return "europe_remote"
        return "remote"
    if "france" in loc:
        return "france"
    return "outside"


def _geo_tier(job: Job, config: dict) -> tuple[int, str]:
    """Returns (bonus_points, reason). Paris/IDF ranks above a named major city,
    which ranks above europe_remote, which ranks above generic other-France
    mobility."""
    tier = geo_tier(job.location, config)
    if tier == "idf":
        return config.get("idf_bonus", 20), f"geo: Paris/IDF ({job.location})"
    if tier == "major_city":
        return config.get("major_city_bonus", 14), f"geo: major French city ({job.location})"
    if tier == "unknown":
        return config.get("unspecified_geo_bonus", 5), "geo: unspecified"
    if tier == "europe_remote":
        bonus = config.get("europe_remote_bonus", 12) if config.get("allow_remote_europe", True) else 0
        return bonus, f"geo: remote at an EU-based employer -- verify visa/payroll eligibility yourself ({job.location})"
    if tier == "france":
        return config.get("other_france_bonus", 10), f"geo: other-France city, opportunistic ({job.location})"
    if tier == "remote":
        bonus = config.get("remote_france_bonus", 8) if config.get("allow_remote_france") else 0
        return bonus, f"geo: remote/France mobility ({job.location})"
    return config.get("outside_france_penalty", -5), f"geo: outside France ({job.location})"


def score(job: Job, config: dict) -> tuple[int, list[str]]:
    reasons: list[str] = []
    text = _text(job)
    pts = 0

    # NLP/CV/PM only -- see scoring.yaml's category_weights comment for why.
    # Credits the best match found, not classify_role()'s single label -- see
    # _category_matches()'s docstring.
    cats = config.get("role_categories") or {}
    identity_cats = {c: cats[c] for c in ("NLP", "CV", "PM") if c in cats}
    weights_cfg = config.get("category_weights") or {}
    default_w = config.get("default_category_weight") or {"title": 15, "body": 8}
    best_pts, best_reason = 0, None
    # Keep only the single highest-scoring match across every category found --
    # not their sum -- so a job matching two categories doesn't get double credit.
    for cat, where in _category_matches(job.title.lower(), text, identity_cats).items():
        w = weights_cfg.get(cat, default_w)
        achieved = w["title"] if where == "title" else w["body"]
        if achieved > best_pts:
            best_pts, best_reason = achieved, f"{cat} role in {where}"
    pts += best_pts
    if best_reason:
        reasons.append(best_reason)

    # AI/ML-DL + general_keywords as a breadth signal, not identity credit --
    # see scoring.yaml's category_weights comment for why.
    depth_terms = [t.lower() for c in ("AI", "ML/DL") for t in cats.get(c, [])]
    depth_terms += [t.lower() for t in config.get("general_keywords", [])]
    matched = [t for t in depth_terms if t in text]
    if matched:
        pts += min(config.get("depth_bonus_cap", 30), config.get("depth_bonus_per_term", 6) * len(matched))
        reasons.append(f"keywords: {', '.join(matched[:5])}")

    geo_bonus, geo_reason = _geo_tier(job, config)
    pts += geo_bonus
    reasons.append(geo_reason)

    langs = [l.lower() for l in config.get("languages", [])]
    if not langs or not job.language or job.language.lower() in langs:
        pts += config.get("language_match_bonus", 5)
    else:
        pts += config.get("language_mismatch_penalty", -10)
        reasons.append(f"language {job.language} off-target")

    senior_hit = next((t for t in SENIOR_TERMS if t in text), None)
    if senior_hit:
        pts += config.get("seniority_signal_penalty", -15)
        reasons.append(f"seniority signal '{senior_hit.strip()}'")

    client_hit = next((t for t in CLIENT_FACING_TERMS if t in text), None)
    if client_hit:
        pts += config.get("client_facing_penalty", -15)
        reasons.append(f"client-facing/consulting signal '{client_hit}'")

    return max(0, min(100, pts)), reasons


_SPECIFIC_ROLE_CATEGORIES = ["NLP", "CV", "AI", "PM"]   # checked before the ML/DL catch-all


def _category_matches(title: str, full_text: str, cats: dict) -> dict[str, str]:
    """category -> "title" | "body" for every category (from the given subset of
    role_categories) with a hit, title checked first. E.g. title="Computer Vision
    Engineer" with full_text also mentioning "sentiment analysis" ->
    {"CV": "title", "NLP": "body"}. Shared by classify_role() (picks one) and
    score() (credits every match, since a job can straddle two categories)."""
    out: dict[str, str] = {}
    for cat, terms in cats.items():
        terms_l = [t.lower() for t in terms]
        if any(t in title for t in terms_l):
            out[cat] = "title"
        elif any(t in full_text for t in terms_l):
            out[cat] = "body"
    return out


def classify_role(title: str, description: str, config: dict) -> str:
    """NLP / CV / AI / ML/DL, checked title-first then title+description. ML/DL
    is a true fallback (returned only when none of the domain-specific categories
    match anywhere) rather than one more entry in the same priority pass -- almost
    every posting's title contains a generic "machine learning" term, so checking
    it at the same tier as the others would make it win by default before the
    description ever gets a look. Takes primitives rather than a Job so the DB
    backfill can call it directly off a stored row."""
    cats = config.get("role_categories") or {}
    matches = _category_matches(title.lower(), f"{title} {description}".lower(), cats)
    for cat in _SPECIFIC_ROLE_CATEGORIES:
        if matches.get(cat) == "title":
            return cat
    for cat in _SPECIFIC_ROLE_CATEGORIES:
        if cat in matches:
            return cat
    return "ML/DL"


def is_relevant(job: Job, config: dict) -> bool:
    """A job must be an ML role by its TITLE, not just be in Paris or mention ML
    in company boilerplate. Guards against non-ML roles from ATS boards."""
    title = job.title.lower()
    cats = config.get("role_categories") or {}
    role_kw = [t.lower() for terms in cats.values() for t in terms]
    return any(k in title for k in role_kw)


def _apply_rules(job: Job, config: dict) -> tuple[list[str], list[int]]:
    """Approved learned rules (config['_active_rules']) that match this job.
    Returns (human reasons, matched rule ids)."""
    text = _text(job)
    company = job.company.lower().strip()
    flags: list[str] = []
    matched: list[int] = []
    for rule in config.get("_active_rules", []):
        kind, value = rule["kind"], rule["value"]
        if kind == "negative_kw" and value in text:
            flags.append(f"rule: '{value}'")
            matched.append(rule["id"])
        elif kind == "company_block" and value == company:
            flags.append(f"rule: company '{value}'")
            matched.append(rule["id"])
    return flags, matched


@dataclass
class Screening:
    score: int
    reasons: str
    keep: bool
    filtered: bool
    filter_reason: str
    seniority: str
    min_years: int | None
    matched_rules: list[int] = field(default_factory=list)
    role_category: str = "ML/DL"


def screen(job: Job, config: dict) -> Screening:
    """Triage one job. keep=False => not stored (excluded / not ML-relevant).
    filtered=True => stored but auto-hidden (senior / too many years / below min_score
    / matched an approved learned rule)."""
    excl = is_excluded(job, config)
    if excl:
        return Screening(0, excl, keep=False, filtered=False, filter_reason=excl,
                         seniority="unknown", min_years=None)
    if not is_relevant(job, config):
        return Screening(0, "not ML-relevant", keep=False, filtered=False,
                         filter_reason="not ML-relevant", seniority="unknown", min_years=None)

    pts, reasons = score(job, config)
    seniority = detect_seniority(job)
    min_years = min_years_required(_text(job))
    role_category = classify_role(job.title, job.description, config)

    sen_cfg = config.get("seniority") or {}
    gate = sen_cfg.get("filter", True)
    max_years = sen_cfg.get("max_years", 3)

    flags: list[str] = []
    if gate and seniority == "senior":
        flags.append("senior title")
    if gate and seniority != "junior" and min_years is not None and min_years > max_years:
        flags.append(f"requires {min_years}+ yrs")
    if config.get("citizenship_gate", True) and has_citizenship_requirement(_text(job)):
        flags.append("citizenship/eligibility requirement")
    if seniority != "junior" and pts < config.get("min_score", 0):
        flags.append(f"score<{config.get('min_score', 0)}")

    rule_flags, matched = _apply_rules(job, config)
    flags += rule_flags

    return Screening(
        score=pts,
        reasons="; ".join(reasons),
        keep=True,
        filtered=bool(flags),
        filter_reason="; ".join(flags),
        seniority=seniority,
        min_years=min_years,
        matched_rules=matched,
        role_category=role_category,
    )
