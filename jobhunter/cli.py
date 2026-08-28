from __future__ import annotations

import argparse

from . import db
from .config import DB_PATH
from .pipeline import run_fetch, tailor_one


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

    if args.command == "web":
        import uvicorn
        uvicorn.run("jobhunter.web.app:app", host=args.host, port=args.port, reload=False)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
