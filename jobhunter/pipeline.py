from __future__ import annotations

import re
import shutil
import time

from . import db, enrich, jd_store, match
from .apply import cover_letter
from .config import load_companies, load_search_config
from .llm import dedup as llm_dedup
from .llm import judge as llm_judge
from .llm import provider
from .notify import dispatch as notify_dispatch
from .sources import ats, ats_discovery, francetravail, hellowork, linkedin, wttj
from .tailor import engine as cv_engine


def _fetch_wttj(config: dict) -> list:
    wt = config.get("wttj") or {}
    queries = wt.get("queries") or [config["query"]]
    jobs: list = []
    for q in queries:
        jobs += wttj.fetch(query=q, max_hits=config.get("max_hits", 100),
                            country=(config.get("countries") or ["France"])[0])
    if wt.get("fetch_europe_remote", True):
        for q in queries:
            jobs += wttj.fetch(query=q, max_hits=wt.get("europe_remote_max_hits", 100),
                                remote_only=True, extra_countries=config.get("europe_countries", []))
    return jobs


def _fetch_linkedin(config: dict) -> list:
    li = config.get("linkedin") or {}
    if not li.get("enabled"):
        return []
    queries = li.get("queries") or [config["query"]]
    jobs = linkedin.fetch(
        queries=queries,
        locations=li.get("locations") or ["Paris, France"],
        max_pages=li.get("max_pages", 5),
        recent_hours=li.get("recent_hours", 168),
        max_retries=li.get("max_retries", 3),
        backoff_base=li.get("backoff_seconds", 2.0),
    )
    er = li.get("europe_remote") or {}
    if er.get("enabled", True):
        jobs += linkedin.fetch(
            queries=queries,
            locations=er.get("locations", ["Europe"]),
            max_pages=er.get("max_pages", 5),
            recent_hours=li.get("recent_hours", 168),
            max_retries=li.get("max_retries", 3),
            backoff_base=li.get("backoff_seconds", 2.0),
            workplace_type=er.get("workplace_type", "2"),
        )
    return jobs


def _fetch_francetravail(config: dict) -> list:
    ft = config.get("francetravail") or {}
    if not ft.get("enabled", True):
        return []
    departements = ft.get("departements", francetravail.IDF_DEPARTEMENTS)
    queries = ft.get("queries") or [config["query"]]
    jobs: list = []
    for q in queries:
        jobs += francetravail.fetch(query=q, departements=departements)
    return jobs


def _fetch_hellowork(config: dict) -> list:
    hw = config.get("hellowork") or {}
    if not hw.get("enabled"):
        return []
    queries = hw.get("queries") or [config["query"]]
    locations = hw.get("locations") or ["Paris"]
    jobs: list = []
    for query in queries:
        for location in locations:
            jobs += hellowork.fetch(query, location)
    return jobs


def _hours_since(timestamp: str) -> float:
    from datetime import datetime, timezone
    then = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def _is_due(conn, name: str, config: dict) -> bool:
    """Each source's own fetch_interval_hours (config/search.yaml, e.g. hellowork's
    24h vs. linkedin's 3h) gates how often it actually fetches, independent of how
    often the outer launchd trigger fires -- see the per-source cadence plan. Unset/0
    = always due (today's behavior for any source that doesn't opt in); no prior
    state (first run, or a brand-new source) is always due."""
    interval = (config.get(name) or {}).get("fetch_interval_hours", 0)
    if not interval:
        return True
    state = db.get_source_fetch_state(conn, name)
    if not state or not state["last_attempted_at"]:
        return True
    return _hours_since(state["last_attempted_at"]) >= interval


def _gather(config: dict, force: bool = False) -> list:
    """Pull every enabled source. Each is isolated: one source's failure (a bad
    token, a network hiccup, a rate-limit) only drops that source's jobs, never
    the whole run. A source not yet due per its own fetch_interval_hours is
    skipped (contributing no jobs this tick) unless force=True -- used for an
    explicit on-demand "check now" that should bypass all cadence gating."""
    sources = [
        ("wttj", lambda: _fetch_wttj(config)),
        ("ats", lambda: ats.fetch_all(load_companies())),
        ("linkedin", lambda: _fetch_linkedin(config)),
        ("francetravail", lambda: _fetch_francetravail(config)),
        ("hellowork", lambda: _fetch_hellowork(config)),
    ]
    jobs: list = []
    counts: dict[str, object] = {}
    with db.connect() as conn:
        for name, fn in sources:
            if not force and not _is_due(conn, name, config):
                interval = config[name]["fetch_interval_hours"]
                state = db.get_source_fetch_state(conn, name)
                remaining = max(0.0, interval - _hours_since(state["last_attempted_at"]))
                counts[name] = f"skipped (next due in ~{remaining:.1f}h)"
                continue
            try:
                got = fn()
            except Exception as exc:
                print(f"  {name} warn: {exc}")
                got = []
            counts[name] = len(got)
            jobs += got
            db.record_source_fetch(conn, name, len(got))
    print(f"  fetched by source: {counts}")
    return jobs


def run_fetch(config: dict | None = None, jobs: list | None = None, force: bool = False) -> dict:
    config = config or load_search_config()
    db.init_db()

    if jobs is None:
        jobs = _gather(config, force=force)

    seen = 0
    kept = 0
    new_ids: list[int] = []
    filtered_new = 0
    per_source: dict[str, int] = {}
    # Geography of ALL new postings this run (filtered included) = the market signal.
    tier_new = {"idf": 0, "france": 0, "remote": 0, "outside": 0, "unknown": 0}
    with db.connect() as conn:
        config = {**config, "_active_rules": [dict(r) for r in db.active_rules(conn)]}
        for job in jobs:
            seen += 1
            s = match.screen(job, config)
            if not s.keep:
                continue
            kept += 1
            tier = match.geo_tier(job.location, config)
            jid, is_new = db.upsert_job(
                conn, job, s.score, s.reasons,
                filtered=s.filtered, filter_reason=s.filter_reason,
                seniority=s.seniority, min_years=s.min_years, geo_tier=tier,
                role_category=s.role_category,
            )
            if is_new:
                tier_new[tier] = tier_new.get(tier, 0) + 1
                for rid in s.matched_rules:
                    db.bump_rule_hits(conn, rid)
                if s.filtered:
                    filtered_new += 1
                else:
                    new_ids.append(jid)
                    per_source[job.source] = per_source.get(job.source, 0) + 1
                    # Pre-create the CV folder as soon as a job clears the filter (i.e.
                    # would show on the dashboard), so the JD (once fetched) and any
                    # tailored CV later land in the same place for offline analysis.
                    out_dir = cv_engine.CV_OUT_DIR / f"{jid}-{cv_engine._slug(job.company)}"
                    out_dir.mkdir(parents=True, exist_ok=True)

        stats = {
            "fetched": seen, "kept": kept, "new": len(new_ids),
            "filtered_new": filtered_new, "new_ids": new_ids, "new_by_source": per_source,
            "new_idf": tier_new["idf"], "new_major_city": tier_new.get("major_city", 0),
            "new_france": tier_new["france"],
            "new_remote": tier_new["remote"], "new_outside": tier_new["outside"],
            "new_europe_remote": tier_new.get("europe_remote", 0),
        }
        db.add_fetch_run(conn, stats)
    return stats


def import_manual_job(title: str, company: str, url: str, location: str = "",
                      description: str = "", contract_type: str = "",
                      posted_at: str = "", config: dict | None = None) -> dict:
    """Hand-add one posting found by manually browsing a company's career site
    (source='manual') through the exact same screen -> upsert path as an
    automated fetch, so it gets scored/geo-tagged/role-categorized identically
    and participates in the normal cross-source dedup (a manually-found LVMH
    posting that's also on LinkedIn merges into that same row, it doesn't
    duplicate it). external_id is the posting URL -- the one thing guaranteed
    unique per real posting for a source with no native id scheme."""
    from .models import Job
    config = config or load_search_config()
    job = Job(source="manual", external_id=url or f"{company}:{title}", title=title,
              company=company, location=location, language="fr", url=url,
              description=description, contract_type=contract_type, posted_at=posted_at)
    with db.connect() as conn:
        config = {**config, "_active_rules": [dict(r) for r in db.active_rules(conn)]}
        s = match.screen(job, config)
        if not s.keep:
            return {"kept": False, "reason": s.filter_reason}
        tier = match.geo_tier(job.location, config)
        jid, is_new = db.upsert_job(
            conn, job, s.score, s.reasons,
            filtered=s.filtered, filter_reason=s.filter_reason,
            seniority=s.seniority, min_years=s.min_years, geo_tier=tier,
            role_category=s.role_category,
        )
        if is_new and not s.filtered:
            out_dir = cv_engine.CV_OUT_DIR / f"{jid}-{cv_engine._slug(job.company)}"
            out_dir.mkdir(parents=True, exist_ok=True)
    return {"kept": True, "job_id": jid, "is_new": is_new, "score": s.score,
            "filtered": s.filtered, "filter_reason": s.filter_reason}


def _auto_tailor_jobs(job_ids: list[int], limit: int) -> int:
    """Tailor a CV + draft a cover letter for up to `limit` of the given job ids,
    skipping any that already have an artifact (idempotency guard). Shared by
    daily_run's inline "new this run" gate and process_backlog's backlog-wide
    sweep. Returns the count that actually compiled."""
    tailored = 0
    for jid in job_ids[:limit]:
        try:
            with db.connect() as conn:
                already = db.list_cv_artifacts(conn, jid)
            if already:
                continue
            print(f"  tailoring #{jid}...")
            result = tailor_one(jid, auto=True)
            cover_one(jid)
            if result.get("compiled"):
                tailored += 1
                print(f"  tailored #{jid}: compiled + cover letter drafted")
            else:
                print(f"  tailored #{jid}: compile failed (see cv.compile.log)")
        except Exception as exc:
            print(f"  auto-tailor warn: job {jid} failed: {exc}")
    return tailored


def daily_run(judge: bool = True, judge_min_score: int = 15, judge_limit: int = 15,
              auto_tailor: bool = True, auto_tailor_limit: int = 10,
              force_fetch: bool = False) -> dict:
    """One scheduled run: fetch everywhere, enrich every new job with real JD content
    (LinkedIn/SmartRecruiters cards carry none up front), re-score with that content,
    THEN LLM-judge the new promising jobs (highest rule-score first, capped to bound
    cost) so the judge sees real descriptions instead of title-only stubs. Jobs the
    judge rates strong/good/stretch (i.e. not an outright "weak" fit) then get a CV
    auto-tailored + a cover letter drafted (capped separately, since each cover letter
    is its own LLM call) so they're ready for you to review and submit yourself --
    never auto-submitted. Returns a summary including the new job rows (for
    notification)."""
    config = load_search_config()
    stats = run_fetch(config, force=force_fetch)

    new_enriched = enrich_new(stats["new_ids"])["enriched"]
    engaged_enriched = enrich_pending(limit=10)["enriched"]

    judged = 0
    qualified: list[int] = []
    if judge and provider.available():
        new_set = set(stats["new_ids"])
        with db.connect() as conn:
            to_judge = [
                r["id"] for r in db.list_jobs(conn, min_score=judge_min_score)
                if r["id"] in new_set and r["llm_score"] is None
            ][:judge_limit]
        for jid in to_judge:
            try:
                result = judge_one(jid)
                judged += 1
                if result.get("skipped"):
                    print(f"  judged #{jid}: skipped ({result['skipped']})")
                else:
                    print(f"  judged #{jid}: {result['verdict']} ({result['score']})")
                if result.get("verdict") in ("strong", "good", "stretch"):
                    qualified.append(jid)
            except Exception as exc:
                print(f"  judge warn: job {jid} failed: {exc}")

    tailored = _auto_tailor_jobs(qualified, auto_tailor_limit) if auto_tailor else 0

    with db.connect() as conn:
        new_rows = [dict(db.get_job(conn, jid)) for jid in stats["new_ids"]]

    notified = notify_dispatch.send(new_rows, config)
    return {**stats, "judged": judged, "tailored": tailored,
            "enriched": new_enriched + engaged_enriched,
            "new_rows": new_rows, "notified": notified}


def process_backlog(judge_min_score: int = 15, judge_limit: int = 10,
                     tailor_limit: int = 10, dedup_limit: int = 10) -> dict:
    """Judge + auto-tailor cycle, decoupled from fetch cadence: sweeps the whole
    backlog (every not-yet-judged job, every judged-but-not-yet-tailored job)
    rather than only the jobs a single run just fetched. Meant to run on its own,
    more frequent cron schedule than the fetch cron -- fetching is the proven,
    LLM-free part of this pipeline; judging/tailoring are the parts that can hit
    an LLM quota, so keeping them on a separate, independently throttleable cron
    means a quota problem only ever stalls this side, never fetching itself."""
    db.init_db()
    if not provider.available():
        return {"enriched": 0, "judged": 0, "skipped_no_description": 0, "tailored": 0,
                "dup_checked": 0, "dup_filtered": 0}

    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_pending_enrichment_any(conn, judge_limit)]
    enriched = 0
    for r in pending:
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")

    judge_stats = judge_all(min_score=judge_min_score, limit=judge_limit)

    with db.connect() as conn:
        candidates = [r["id"] for r in db.jobs_ready_for_auto_tailor(conn, tailor_limit)]
    tailored = _auto_tailor_jobs(candidates, tailor_limit)

    dup_stats = check_duplicates(limit=dedup_limit)

    return {"enriched": enriched, "judged": judge_stats["judged"],
            "skipped_no_description": judge_stats["skipped_no_description"],
            "tailored": tailored,
            "dup_checked": dup_stats["checked"], "dup_filtered": dup_stats["filtered"]}


def check_duplicates(limit: int = 10) -> dict:
    """LLM-compare heuristically-flagged possible-duplicate pairs (db.find_possible_duplicates)
    using their JD text, cache the verdict so a pair is never re-checked, and auto-filter
    the older side of a high-confidence 'same' verdict (the newer listing is more likely
    still open). Skips any pair where either side still lacks real JD content -- a
    title-only guess isn't reliable enough for a call this consequential."""
    db.init_db()
    if not provider.available():
        return {"checked": 0, "same": 0, "filtered": 0}

    with db.connect() as conn:
        pairs = db.find_possible_duplicates(conn)
        todo = []
        seen = set()
        for p in pairs:
            a, b = sorted((p["a"], p["b"]))
            if (a, b) in seen or db.get_duplicate_check(conn, a, b) is not None:
                continue
            seen.add((a, b))
            row_a, row_b = db.get_job(conn, a), db.get_job(conn, b)
            if not row_a or not row_b:
                continue
            if (len((row_a["description"] or "").strip()) < _MIN_DESCRIPTION_CHARS or
                    len((row_b["description"] or "").strip()) < _MIN_DESCRIPTION_CHARS):
                continue
            todo.append((a, b))
    todo = todo[:limit]

    checked = same = filtered = 0
    for a, b in todo:
        with db.connect() as conn:
            job_a = db.job_from_row(db.get_job(conn, a))
            job_b = db.job_from_row(db.get_job(conn, b))
        try:
            result = llm_dedup.compare(job_a, job_b)
        except Exception as exc:
            print(f"  dedup warn: #{a} vs #{b} failed: {exc}")
            continue
        checked += 1
        with db.connect() as conn:
            db.record_duplicate_check(conn, a, b, result["verdict"], result["confidence"], result["reason"])
            if result["verdict"] == "same" and result["confidence"] == "high":
                same += 1
                row_a, row_b = db.get_job(conn, a), db.get_job(conn, b)
                older, newer = (a, b) if row_a["fetched_at"] <= row_b["fetched_at"] else (b, a)
                db.set_llm_filter(conn, older, f"llm dedup: same posting as #{newer} -- {result['reason']}")
                filtered += 1
    return {"checked": checked, "same": same, "filtered": filtered}


def enrich_one(job_id: int) -> dict:
    """Fetch the full description for one job, store it, and re-score with that content
    (title-only scoring becomes content-aware once the JD lands -- this can also move a
    job into/out of the Filtered bucket, e.g. a '5+ years' requirement only visible in
    the body)."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        source, ext, url = row["source"], row["external_id"], row["url"]
        title, company = row["title"], row["company"]
    text = enrich.fetch_full_text(source, ext, url)
    if not text:
        with db.connect() as conn:
            db.bump_enrich_attempts(conn, job_id)
        return {"job_id": job_id, "enriched": False}
    jd_path = jd_store.save_jd(source=source, external_id=ext, title=title, company=company,
                                url=url, description=text)
    config = load_search_config()
    with db.connect() as conn:
        db.set_description(conn, job_id, text)
        job = db.job_from_row(db.get_job(conn, job_id))
        cfg = {**config, "_active_rules": [dict(r) for r in db.active_rules(conn)]}
        s = match.screen(job, cfg)
        db.update_screening(conn, job_id, s.score, s.reasons, filtered=s.filtered,
                            filter_reason=s.filter_reason, seniority=s.seniority,
                            min_years=s.min_years, role_category=s.role_category)
    if not s.filtered:
        # Keep a copy of the JD right next to where the tailored CV will land,
        # so both are in one place for later analysis.
        out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(company)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(jd_path, out_dir / "jd.txt")
    return {"job_id": job_id, "enriched": True, "chars": len(text),
            "score": s.score, "filtered": s.filtered}


def enrich_pending(limit: int = 20) -> dict:
    """Enrich engaged jobs (interested / past 'new') that lack a full description."""
    db.init_db()
    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_needing_enrichment(conn, limit)]
    enriched = 0
    for r in pending:
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")
    return {"candidates": len(pending), "enriched": enriched}


def enrich_new(job_ids: list[int]) -> dict:
    """Enrich every job from this run's fresh crop that still lacks a real description
    (LinkedIn guest cards and SmartRecruiters give none up front; WTTJ's profile field
    is sometimes empty). Unlike enrich_pending this isn't gated on engagement -- every
    new posting gets its full JD saved locally, which is what backs the rule-score
    content signal, the LLM judge, and any downstream corpus (e.g. embeddings) built
    from the DB. Throttled since it now includes LinkedIn's guest endpoint
    unconditionally rather than only for jobs you've engaged with."""
    db.init_db()
    with db.connect() as conn:
        pending = [dict(r) for r in db.jobs_by_id_needing_enrichment(conn, job_ids)]
    enriched = 0
    for i, r in enumerate(pending):
        if i:
            time.sleep(1.0)
        try:
            if enrich_one(r["id"]).get("enriched"):
                enriched += 1
        except Exception as exc:
            print(f"  enrich warn: job {r['id']} failed: {exc}")
    return {"candidates": len(pending), "enriched": enriched}


# Below this, there's not enough real JD text to judge on -- title/company alone
# (LinkedIn guest cards, thin WTTJ profiles) makes the LLM guess rather than assess,
# and that guess tends to read as more confident/optimistic than it should.
_MIN_DESCRIPTION_CHARS = 100


def _maybe_discover_ats(conn, company: str) -> None:
    """Once a company has a good/strong verdict, see if it already runs a public
    ATS board we could fetch from directly instead of relying on LinkedIn/HelloWork
    scraping. Only probes a company once (skips if already in companies.yaml or
    already on the target_companies checklist) -- a hit is staged as an unconfirmed
    target_companies result, never written straight to companies.yaml, since a
    guessed slug can coincidentally collide with an unrelated company's real
    board (see ats_discovery.probe's docstring)."""
    known = {c["name"].strip().lower() for c in load_companies()}
    if company.strip().lower() in known:
        return
    if not db.add_target_company(conn, company):
        return  # already on the checklist -- don't re-probe every good verdict
    row = conn.execute(
        "SELECT id FROM target_companies WHERE LOWER(name) = LOWER(?)", (company,)
    ).fetchone()
    try:
        hit = ats_discovery.probe(company)
    except Exception as exc:
        hit = None
        print(f"  ats_discovery warn: {company}: {exc}")
    db.mark_company_checked(conn, row["id"], hit or "no ATS match found (auto-probed)")


def judge_one(job_id: int) -> dict:
    """LLM fit-judge one job; store score/verdict/reasons on the job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
        profile = db.current_profile(conn)
    if len((job.description or "").strip()) < _MIN_DESCRIPTION_CHARS:
        return {"job_id": job_id, "skipped": "no real JD content yet"}
    result = llm_judge.judge(job, preferences=profile["text"] if profile else "")
    with db.connect() as conn:
        db.set_llm_judgment(conn, job_id, result["score"], result["verdict"], result["reasons"])
        if result.get("seniority") or result.get("min_years") is not None:
            db.set_seniority(conn, job_id, result.get("seniority", ""), result.get("min_years"))
        if result["verdict"] == "weak":
            # A weak verdict is a more informed signal than the rule score that got it
            # onto the board in the first place -- auto-hide it like any other filter.
            db.set_llm_filter(conn, job_id, "llm judge: weak fit")
        elif result["verdict"] in ("good", "strong"):
            _maybe_discover_ats(conn, job.company)
    return {"job_id": job_id, **result}


_JUNIOR_MIN_SCORE_REASON = "score<"
_LLM_WEAK_FILTER_REASON = "llm judge: weak fit"
_SCORE_REASON_RE = re.compile(r"^score<\d+$")


def _only_stale_gate_reasons(reason: str) -> bool:
    """True if every '; '-joined flag in a filter_reason is one a rejudge can make
    moot (the old weak verdict, or the old rule-based min_score gate) -- i.e. safe
    to unhide on an improved verdict. A job also blocked by anything else (a
    citizenship hard disqualifier, a learned rule, ...) must stay filtered."""
    parts = [p.strip() for p in reason.split(";") if p.strip()]
    return all(p == _LLM_WEAK_FILTER_REASON or _SCORE_REASON_RE.match(p) for p in parts)


def _rejudge_weak_verdicts(where_sql: str, params: tuple, limit: int | None) -> dict:
    """Shared engine behind rejudge_juniors/rejudge_category: rejudge every
    llm_verdict='weak' job matching an extra WHERE clause, unhiding on an improved
    verdict only if the old filter_reason was solely made-moot flags (see
    _only_stale_gate_reasons). Skips dismissed/interested jobs -- never overrides
    a human label."""
    label_filter = "AND COALESCE(user_label,'') NOT IN ('dismissed','interested')"
    with db.connect() as conn:
        weak_rows = conn.execute(
            f"SELECT id, filter_reason FROM jobs WHERE llm_verdict = 'weak' {where_sql} {label_filter}",
            params,
        ).fetchall()
    old_reason_by_id = {r["id"]: (r["filter_reason"] or "").strip() for r in weak_rows}
    ids = list(old_reason_by_id)
    if limit:
        ids = ids[:limit]

    rejudged = unfiltered = 0
    for jid in ids:
        result = judge_one(jid)
        rejudged += 1
        if (result.get("verdict") and result["verdict"] != "weak"
                and _only_stale_gate_reasons(old_reason_by_id[jid])):
            with db.connect() as conn:
                db.set_filtered(conn, jid, False, "")
            unfiltered += 1
    return {"rejudged": rejudged, "unfiltered": unfiltered}


def rejudge_juniors(limit: int | None = None) -> dict:
    """One-off catch-up for junior-titled postings screened/judged before the
    junior exemptions landed (match.py's min_score exemption, judge.py's
    'Junior/entry-level exception'). Two groups:
      * rule-filtered by the old min_score gate, never reached the LLM -- rescreen
        with match.screen() and unhide if the new (exempt) result says keep.
      * already judged 'weak' under the old, stricter judge prompt -- rejudge with
        judge_one() and unhide only if the new verdict isn't weak AND the job was
        filtered solely for the old weak verdict (never touches a job also filtered
        for an unrelated reason, e.g. a citizenship hard disqualifier).
    Skips dismissed/interested jobs entirely -- never overrides a human label."""
    db.init_db()
    config = load_search_config()
    label_filter = "AND COALESCE(user_label,'') NOT IN ('dismissed','interested')"

    with db.connect() as conn:
        pre_judge_rows = conn.execute(
            f"""SELECT * FROM jobs WHERE llm_score IS NULL AND filtered = 1
                AND seniority = 'junior' AND filter_reason LIKE ? {label_filter}""",
            (f"%{_JUNIOR_MIN_SCORE_REASON}%",),
        ).fetchall()

    unfiltered = 0
    for row in pre_judge_rows:
        job = db.job_from_row(row)
        s = match.screen(job, config)
        if not s.filtered:
            with db.connect() as conn:
                db.update_screening(conn, row["id"], s.score, s.reasons, filtered=False,
                                    filter_reason="", seniority=s.seniority,
                                    min_years=s.min_years, role_category=s.role_category)
            unfiltered += 1

    weak = _rejudge_weak_verdicts("AND seniority = 'junior'", (), limit)
    unfiltered += weak["unfiltered"]
    return {"rescreened": len(pre_judge_rows), "rejudged": weak["rejudged"], "unfiltered": unfiltered}


def rejudge_category(role_category: str, limit: int | None = None) -> dict:
    """One-off catch-up: rejudge every job in a role_category that's currently
    judged 'weak', for after a judge-prompt or scoring-config change that should
    apply retroactively (e.g. treating Computer Vision as a co-equal specialization
    rather than an off-target domain, or expanding boost_keywords/role_categories
    coverage via rescreen_all). Same unfilter-safety rule as rejudge_juniors --
    only unhides a job whose old filter_reason was solely stale weak-verdict/
    score-gate flags. Skips dismissed/interested jobs."""
    db.init_db()
    return _rejudge_weak_verdicts("AND role_category = ?", (role_category,), limit)


def rescreen_all() -> dict:
    """Re-run rule-based screening for every stored job against the *current*
    config -- for catching up after a role_keywords/boost_keywords/role_categories
    change (e.g. expanding Computer Vision's keyword coverage) that should have
    applied retroactively. Jobs the LLM has already judged (llm_score set) keep
    their score/filtered status untouched -- the LLM verdict is the authoritative
    signal there (see judge_one/set_llm_filter), a rule-score refresh must never
    override it -- only their role_category label is refreshed (cosmetic, changes
    only the dashboard's Category column). Pre-judgment jobs get a full rescreen:
    score, reasons, filtered, role_category."""
    db.init_db()
    config = load_search_config()

    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()

    rescreened = unfiltered = newly_filtered = recategorized = 0
    category_counts: dict[str, int] = {}
    for row in rows:
        job = db.job_from_row(row)
        s = match.screen(job, config)
        if row["llm_score"] is None:
            if (row["score"], bool(row["filtered"]), row["role_category"]) == \
                    (s.score, s.filtered, s.role_category):
                continue
            was_filtered = bool(row["filtered"])
            with db.connect() as conn:
                db.update_screening(conn, row["id"], s.score, s.reasons, filtered=s.filtered,
                                    filter_reason=s.filter_reason, seniority=s.seniority,
                                    min_years=s.min_years, role_category=s.role_category)
            rescreened += 1
            if was_filtered and not s.filtered:
                unfiltered += 1
            elif not was_filtered and s.filtered:
                newly_filtered += 1
        elif row["role_category"] != s.role_category:
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET role_category = ? WHERE id = ?",
                            (s.role_category, row["id"]))
            recategorized += 1
        else:
            continue
        category_counts[s.role_category] = category_counts.get(s.role_category, 0) + 1

    return {"rescreened": rescreened, "unfiltered": unfiltered, "newly_filtered": newly_filtered,
            "recategorized": recategorized, "category_counts": category_counts}


def _permanently_unfetchable(row) -> bool:
    """True once a job has exhausted every enrichment retry (see MAX_ENRICH_ATTEMPTS)
    and still has no real JD text -- it will never pass judge_one's description-length
    gate, so leaving it in the queue would just have it silently reoccupy a limit slot
    every single run, forever, ahead of anything genuinely judgeable."""
    return (not row["description_full"]
            and (row["enrich_attempts"] or 0) >= db.MAX_ENRICH_ATTEMPTS)


def judge_all(min_score: int = 40, limit: int | None = None) -> dict:
    """Judge every stored job at/above a rule-score threshold that isn't judged yet.
    Skips jobs with no real JD content rather than burning a call on a title-only guess --
    see judge_one's _MIN_DESCRIPTION_CHARS gate. Jobs that will never get real JD content
    (enrichment permanently exhausted) are excluded from the queue entirely, rather than
    just skipped call-by-call -- otherwise a high rule-score but permanently-unfetchable
    job sits at the front of the score-ordered queue forever, crowding out real candidates
    behind it every run."""
    db.init_db()
    with db.connect() as conn:
        rows = [r for r in db.list_jobs(conn, min_score=min_score)
                if r["llm_score"] is None and not _permanently_unfetchable(r)]
    if limit:
        rows = rows[:limit]
    judged = 0
    skipped = 0
    for r in rows:
        try:
            result = judge_one(r["id"])
            if result.get("skipped"):
                skipped += 1
                print(f"  judged #{r['id']}: skipped ({result['skipped']})")
            else:
                judged += 1
                print(f"  judged #{r['id']}: {result['verdict']} ({result['score']})")
        except Exception as exc:
            print(f"  judge warn: job {r['id']} failed: {exc}")
    return {"candidates": len(rows), "judged": judged, "skipped_no_description": skipped}


def _judge_context(row) -> str | None:
    """The fit-judge's own verdict/reasons for this job, formatted as background
    for the tailor/cover-letter prompts -- already computed and stored on the
    job by judge_one, reused here rather than re-derived from scratch."""
    if not row["llm_verdict"]:
        return None
    return f"Rated '{row['llm_verdict']}' fit ({row['llm_score']}/100): {row['llm_reasons']}"


def cover_one(job_id: int) -> dict:
    """Draft a cover letter for one job; store the file path on the application."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)
    out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(job.company)}"
    path = cover_letter.draft_to_file(job, out_dir, judge_context=_judge_context(row))
    with db.connect() as conn:
        db.set_cover_letter(conn, job_id, str(path))
    return {"job_id": job_id, "cover_letter": str(path)}


def tailor_one(job_id: int, auto: bool = False) -> dict:
    """Tailor + compile a CV for one job, record it, and mark the job cv_ready.
    `auto=True` (daily_run's unsupervised path) also enforces the exact-2-page
    rule -- see cv_engine.tailor_job."""
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        job = db.job_from_row(row)

    tex_path, pdf_path = cv_engine.tailor_job(job, job_id, auto=auto, judge_context=_judge_context(row),
                                              role_category=row["role_category"] or "")

    with db.connect() as conn:
        db.add_cv_artifact(conn, job_id, str(tex_path), str(pdf_path or ""),
                           base_version="cv_base.tex", origin="ai")
        if pdf_path:
            db.update_status(conn, job_id, "cv_ready")
    return {
        "job_id": job_id,
        "tex": str(tex_path),
        "pdf": str(pdf_path) if pdf_path else None,
        "compiled": pdf_path is not None,
    }


def import_revised_cv(job_id: int, pdf: "Path | bytes", tex: "Path | None" = None) -> dict:
    """Store a human-revised CV against a job. It becomes the active CV (latest-wins)
    without touching the AI versions, which stay on disk. `pdf` is a path or raw bytes."""
    from pathlib import Path
    import shutil
    db.init_db()
    with db.connect() as conn:
        row = db.get_job(conn, job_id)
        if not row:
            return {"job_id": job_id, "error": "not found"}
        company = row["company"]

    out_dir = cv_engine.CV_OUT_DIR / f"{job_id}-{cv_engine._slug(company)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    pdf_dest = out_dir / f"revised-{stamp}.pdf"
    if isinstance(pdf, (bytes, bytearray)):
        pdf_dest.write_bytes(pdf)
    else:
        shutil.copy(Path(pdf), pdf_dest)

    tex_dest = ""
    if tex is not None:
        tex_dest_path = out_dir / f"revised-{stamp}.tex"
        shutil.copy(Path(tex), tex_dest_path)
        tex_dest = str(tex_dest_path)

    with db.connect() as conn:
        db.add_cv_artifact(conn, job_id, tex_dest, str(pdf_dest),
                           base_version="revised", origin="revised")
        db.update_status(conn, job_id, "cv_ready")
    return {"job_id": job_id, "pdf": str(pdf_dest), "tex": tex_dest or None, "origin": "revised"}


def _timestamp() -> str:
    """A filesystem-safe timestamp. Isolated so tests can monkeypatch it deterministically."""
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")
