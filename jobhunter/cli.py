from __future__ import annotations

import argparse

from . import db, schedule
from .config import DB_PATH
from .llm import provider
from .pipeline import cover_one, daily_run, judge_all, judge_one, run_fetch, tailor_one


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunter", description="Paris ML job hunter")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="fetch + score + store new jobs")
    sub.add_parser("init", help="create the database")

    p_list = sub.add_parser("list", help="list stored jobs")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--min-score", type=int, default=0)

    p_tailor = sub.add_parser("tailor", help="generate + compile a tailored CV")
    p_tailor.add_argument("job_id", type=int)

    p_judge = sub.add_parser("judge", help="LLM fit-judge jobs")
    p_judge.add_argument("job_id", type=int, nargs="?", help="omit to judge all above --min-score")
    p_judge.add_argument("--min-score", type=int, default=40)
    p_judge.add_argument("--limit", type=int, default=None)

    p_cover = sub.add_parser("cover", help="draft a cover letter (LLM)")
    p_cover.add_argument("job_id", type=int)

    sub.add_parser("llm-status", help="show which LLM backend is active")

    p_run = sub.add_parser("run", help="one scheduled run: fetch + judge new jobs (cron target)")
    p_run.add_argument("--no-judge", action="store_true")

    p_cron = sub.add_parser("cron", help="manage the daily crontab entry")
    p_cron.add_argument("action", choices=["show", "install", "uninstall"], nargs="?", default="show")
    p_cron.add_argument("--time", default="08:00", help="HH:MM (default 08:00)")

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
        print(f"fetched={stats['fetched']} kept={stats['kept']} new={stats['new']}")
        if stats.get("new_by_source"):
            print("  new by source:", dict(stats["new_by_source"]))
        return 0

    if args.command == "list":
        db.init_db()
        with db.connect() as conn:
            rows = db.list_jobs(conn, status=args.status, min_score=args.min_score)
            for r in rows:
                print(f"[{r['score']:3d}] {r['status']:11s} {r['title'][:55]:55s} @ {r['company'][:25]:25s} {r['location'][:20]}")
            print(f"\n{len(rows)} jobs")
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

    if args.command == "run":
        summary = daily_run(judge=not args.no_judge)
        print(f"fetched={summary['fetched']} kept={summary['kept']} new={summary['new']} judged={summary['judged']}")
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

    if args.command == "web":
        import uvicorn
        uvicorn.run("jobhunter.web.app:app", host=args.host, port=args.port, reload=False)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
