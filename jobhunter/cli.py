from __future__ import annotations

import argparse

from . import db, learn, schedule
from .config import DB_PATH, load_search_config
from .llm import provider
from .notify import dispatch as notify_dispatch
from .pipeline import (cover_one, daily_run, enrich_one, enrich_pending, judge_all,
                       judge_one, run_fetch, tailor_one)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunter", description="Paris ML job hunter")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="fetch + score + store new jobs")
    sub.add_parser("init", help="create the database")

    p_list = sub.add_parser("list", help="list stored jobs")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--min-score", type=int, default=0)
    p_list.add_argument("--filtered", action="store_true", help="show the auto-hidden Filtered bucket")
    p_list.add_argument("--dismissed", action="store_true", help="show jobs you explicitly dismissed")

    p_fb = sub.add_parser("feedback", help="record your judgment on a job")
    p_fb.add_argument("job_id", type=int)
    p_fb.add_argument("--label", choices=["interested", "dismissed", "clear"], required=True)
    p_fb.add_argument("--reasons", default="", help="comma tags for dismissal (e.g. too_senior,location)")

    p_tailor = sub.add_parser("tailor", help="generate + compile a tailored CV")
    p_tailor.add_argument("job_id", type=int)

    p_judge = sub.add_parser("judge", help="LLM fit-judge jobs")
    p_judge.add_argument("job_id", type=int, nargs="?", help="omit to judge all above --min-score")
    p_judge.add_argument("--min-score", type=int, default=40)
    p_judge.add_argument("--limit", type=int, default=None)

    p_cover = sub.add_parser("cover", help="draft a cover letter (LLM)")
    p_cover.add_argument("job_id", type=int)

    p_enrich = sub.add_parser("enrich", help="fetch full descriptions for engaged jobs")
    p_enrich.add_argument("job_id", type=int, nargs="?", help="omit to enrich all pending engaged jobs")
    p_enrich.add_argument("--limit", type=int, default=20)

    sub.add_parser("llm-status", help="show which LLM backend is active")

    p_run = sub.add_parser("run", help="one scheduled run: fetch + judge new jobs (cron target)")
    p_run.add_argument("--no-judge", action="store_true")

    p_cron = sub.add_parser("cron", help="manage the daily crontab entry")
    p_cron.add_argument("action", choices=["show", "install", "uninstall"], nargs="?", default="show")
    p_cron.add_argument("--time", default="08:00", help="HH:MM (default 08:00)")

    p_notify = sub.add_parser("notify", help="send a digest of current top jobs to configured channels")
    p_notify.add_argument("--min-score", type=int, default=None, help="override notifications.min_score")

    p_rules = sub.add_parser("rules", help="learn / review filter rules from your feedback")
    p_rules.add_argument("action", choices=["mine", "list", "approve", "reject", "add"], default="list", nargs="?")
    p_rules.add_argument("rule_id", type=int, nargs="?", help="rule id for approve/reject")
    p_rules.add_argument("--show", choices=["all", "pending", "active"], default="all")
    p_rules.add_argument("--kind", choices=list(db.RULE_KINDS), help="for add")
    p_rules.add_argument("--value", help="for add")
    p_rules.add_argument("--weight", type=int, default=20)

    p_web = sub.add_parser("web", help="run the dashboard")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "init":
        db.init_db()
        print(f"initialized db at {DB_PATH}")
        return 0

    if args.command == "fetch":
        stats = run_fetch()
        print(f"fetched={stats['fetched']} kept={stats['kept']} new={stats['new']} "
              f"filtered_new={stats.get('filtered_new', 0)}")
        if stats.get("new_by_source"):
            print("  new by source:", dict(stats["new_by_source"]))
        return 0

    if args.command == "list":
        db.init_db()
        with db.connect() as conn:
            if args.dismissed:
                rows = db.list_jobs(conn, status=args.status, min_score=args.min_score,
                                    filtered=None, dismissed=True)
            else:
                rows = db.list_jobs(conn, status=args.status, min_score=args.min_score,
                                    filtered=1 if args.filtered else 0, dismissed=False)
            for r in rows:
                if args.dismissed and r["dismiss_reasons"]:
                    tail = f"  <{r['dismiss_reasons']}>"
                elif args.filtered and r["filter_reason"]:
                    tail = f"  <{r['filter_reason']}>"
                else:
                    tail = ""
                print(f"[{r['score']:3d}] {r['status']:11s} {r['title'][:55]:55s} @ {r['company'][:25]:25s} {r['location'][:20]}{tail}")
            label = "dismissed jobs" if args.dismissed else ("filtered jobs" if args.filtered else "jobs")
            print(f"\n{len(rows)} {label}")
        return 0

    if args.command == "feedback":
        db.init_db()
        label = "" if args.label == "clear" else args.label
        with db.connect() as conn:
            db.set_feedback(conn, args.job_id, label, args.reasons)
        print(f"job {args.job_id}: label={label or 'cleared'}"
              + (f" reasons={args.reasons}" if args.reasons else ""))
        return 0

    if args.command == "tailor":
        result = tailor_one(args.job_id)
        if result.get("error"):
            print(f"error: {result['error']}")
            return 1
        print(f"tex: {result['tex']}")
        print(f"pdf: {result['pdf']}  (compiled={result['compiled']})")
        return 0

    if args.command == "llm-status":
        print(f"LLM backend: {provider.backend()} (available={provider.available()})")
        return 0

    if args.command == "judge":
        if args.job_id is not None:
            result = judge_one(args.job_id)
            if result.get("error"):
                print(f"error: {result['error']}"); return 1
            print(f"[{result['score']:3d}] {result['verdict']:8s} {result['reasons']}")
        else:
            stats = judge_all(min_score=args.min_score, limit=args.limit)
            print(f"candidates={stats['candidates']} judged={stats['judged']}")
        return 0

    if args.command == "cover":
        result = cover_one(args.job_id)
        if result.get("error"):
            print(f"error: {result['error']}"); return 1
        print(f"cover letter: {result['cover_letter']}")
        return 0

    if args.command == "enrich":
        if args.job_id is not None:
            result = enrich_one(args.job_id)
            if result.get("error"):
                print(f"error: {result['error']}"); return 1
            print(f"enriched={result['enriched']}"
                  + (f" chars={result['chars']}" if result.get("enriched") else ""))
        else:
            stats = enrich_pending(limit=args.limit)
            print(f"candidates={stats['candidates']} enriched={stats['enriched']}")
        return 0

    if args.command == "run":
        summary = daily_run(judge=not args.no_judge)
        print(f"fetched={summary['fetched']} kept={summary['kept']} new={summary['new']} "
              f"filtered_new={summary.get('filtered_new', 0)} judged={summary['judged']} "
              f"enriched={summary.get('enriched', 0)}")
        if summary.get("new_by_source"):
            print("  new by source:", dict(summary["new_by_source"]))
        return 0

    if args.command == "cron":
        try:
            hour, minute = (int(x) for x in args.time.split(":"))
        except ValueError:
            print("--time must be HH:MM"); return 1
        if args.action == "install":
            line = schedule.install(hour, minute)
            print(f"installed:\n  {line}")
        elif args.action == "uninstall":
            print("removed" if schedule.uninstall() else "no jobhunter entry found")
        else:
            cur = schedule.current()
            print(f"current:\n  {cur}" if cur else "not installed")
            print(f"\nwould install:\n  {schedule.cron_line(hour, minute)}")
        return 0

    if args.command == "notify":
        db.init_db()
        cfg = load_search_config()
        notif = cfg.get("notifications") or {}
        min_score = args.min_score if args.min_score is not None else notif.get("min_score", 60)
        with db.connect() as conn:
            rows = [dict(r) for r in db.list_jobs(conn, min_score=0)]
        result = notify_dispatch.send(rows, {**cfg, "notifications": {**notif, "min_score": min_score}})
        print(f"selected={result['selected']} results={result.get('results', {})}")
        return 0

    if args.command == "rules":
        db.init_db()
        if args.action == "mine":
            with db.connect() as conn:
                result = learn.mine_rules(conn)
            if result["status"] == "insufficient":
                print(f"not enough feedback yet: {result['dismissed']} dismissed "
                      f"(need {result['need']}). Dismiss more jobs, then re-run.")
                return 0
            print(f"from {result['dismissed']} dismissed / {result['interested']} interested: "
                  f"{result['suggested']} candidates, {result['new']} new (inactive, pending your approval)")
            for c in result["rules"][:15]:
                print(f"  [{c['score']:.2f}] {c['kind']:13s} {c['value']:30s} ({c['evidence']})")
            print("\napprove with: jobhunter rules approve <id>   (see: jobhunter rules list)")
            return 0
        if args.action in ("approve", "reject"):
            if args.rule_id is None:
                print(f"{args.action} needs a rule id"); return 1
            with db.connect() as conn:
                if args.action == "approve":
                    db.set_rule_active(conn, args.rule_id, 1)
                else:
                    db.delete_rule(conn, args.rule_id)
            print(f"rule {args.rule_id} {'approved (active)' if args.action == 'approve' else 'rejected (deleted)'}")
            return 0
        if args.action == "add":
            if not args.kind or not args.value:
                print("add needs --kind and --value"); return 1
            with db.connect() as conn:
                ok = db.add_rule(conn, args.kind, args.value, source="manual",
                                 weight=args.weight, evidence="manual", active=1)
            print("added (active)" if ok else "already exists")
            return 0
        active = {"pending": 0, "active": 1, "all": None}[args.show]
        with db.connect() as conn:
            rows = db.list_rules(conn, active=active)
        for r in rows:
            mark = "✓" if r["active"] else "·"
            print(f"  {mark} #{r['id']:<3d} [{r['source']:7s}] {r['kind']:13s} {r['value']:30s} "
                  f"w={r['weight']:<3d} hits={r['hit_count']:<3d} {r['evidence']}")
        print(f"\n{len(rows)} rules ({args.show})")
        return 0

    if args.command == "web":
        import uvicorn
        uvicorn.run("jobhunter.web.app:app", host=args.host, port=args.port, reload=False)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
