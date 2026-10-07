"""Does slim mode judge duplicate pairs as well as the default setup?

Re-judges pairs the LLM already judged (default setup, stored in duplicate_checks) with
dedup.compare in slim mode and compares the verdicts. Live LLM calls, so run it by hand:

    python scripts/eval_dedup_slim.py            # 20 pairs
    python scripts/eval_dedup_slim.py --n 12

Pass rule: every stored same/high pair stays "same", no stored different/high pair flips to
"same" (that direction would hide a job), and overall verdict agreement is at least 90%.
Exit code 0 on pass, 1 on fail. Writes nothing to the database.
"""
from __future__ import annotations

import argparse
import random
import sys

from jobhunter import db
from jobhunter.llm import dedup

MAX_PAIRS = 20          # live-LLM runs stay small
MIN_AGREEMENT = 0.90


def pick_pairs(conn, n: int, seed: int = 7) -> list[dict]:
    """About 40% same/high, 40% different/high, the rest medium-confidence ones, all judged by the
    LLM (not by the identical-text rule) and both descriptions present."""
    rows = [dict(r) for r in conn.execute(
        "SELECT d.* FROM duplicate_checks d "
        "JOIN jobs a ON a.id = d.job_id_a JOIN jobs b ON b.id = d.job_id_b "
        "WHERE d.reason NOT LIKE 'the descriptions are%' "
        "AND length(a.description) >= 300 AND length(b.description) >= 300")]
    rng = random.Random(seed)
    strata = {
        "same/high": [r for r in rows if r["verdict"] == "same" and r["confidence"] == "high"],
        "different/high": [r for r in rows if r["verdict"] == "different" and r["confidence"] == "high"],
        "medium": [r for r in rows if r["confidence"] != "high"],
    }
    share = {"same/high": round(n * 0.4), "different/high": round(n * 0.4)}
    share["medium"] = n - sum(share.values())
    picked = []
    for name, pool in strata.items():
        picked += rng.sample(pool, min(share[name], len(pool)))
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=MAX_PAIRS)
    args = ap.parse_args()
    n = min(args.n, MAX_PAIRS)

    db.init_db()
    with db.connect() as conn:
        pairs = pick_pairs(conn, n)
        jobs = {(p["job_id_a"], p["job_id_b"]): (db.job_from_row(db.get_job(conn, p["job_id_a"])),
                                                   db.job_from_row(db.get_job(conn, p["job_id_b"])))
                for p in pairs}

    agree = flips_to_same = lost_same = 0
    print(f"{'pair':>12}  {'stored':<18} {'slim':<18} result")
    for p in pairs:
        a, b = jobs[(p["job_id_a"], p["job_id_b"])]
        got = dedup.compare(a, b, slim=True)
        stored = f"{p['verdict']}/{p['confidence']}"
        slim = f"{got['verdict']}/{got['confidence']}"
        same_verdict = got["verdict"] == p["verdict"]
        agree += same_verdict
        note = "ok" if same_verdict else "DIFFERENT"
        if not same_verdict and got["verdict"] == "same":
            flips_to_same += 1
        if not same_verdict and p["verdict"] == "same" and p["confidence"] == "high":
            lost_same += 1
        print(f"{p['job_id_a']:>5}-{p['job_id_b']:<6}  {stored:<18} {slim:<18} {note}")
        if not same_verdict:
            print(f"      stored reason: {p['reason'][:140]}\n      slim reason:   {got['reason'][:140]}")

    total = len(pairs)
    rate = agree / total if total else 0.0
    print(f"\nagreement {agree}/{total} = {rate:.0%}; flips to 'same': {flips_to_same}; "
          f"stored same/high lost: {lost_same}")
    passed = total > 0 and rate >= MIN_AGREEMENT and flips_to_same == 0 and lost_same == 0
    print("PASS: slim judges as well -> llm.dedup_slim can be turned on" if passed
          else "FAIL: keep llm.dedup_slim off")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
