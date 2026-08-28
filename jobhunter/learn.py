"""Learn candidate filter rules from your explicit feedback.

Ground truth is explicit-only: dismissed jobs are negatives, interested jobs are
positives. The miner surfaces terms (and companies) that discriminate dismissals
from interests and proposes them as *inactive* rules — you approve before any rule
affects screening (Phase 5). Deliberately simple and transparent: document-frequency
difference, not a black box, so every suggestion carries its own evidence.
"""
from __future__ import annotations

import re
from collections import Counter

from . import db

# Ubiquitous role/geo/format tokens carry no signal (they appear everywhere) — drop them.
_STOP = set("""
a an the and or of to in for with on at by as is are be we you our your their this that
de la le les des du un une et en pour dans sur avec au aux par ou nos vos leur
machine learning engineer engineering data scientist science ml ai artificial intelligence
developer software senior junior confirme confirmé lead staff principal expert
paris france ile idf remote hybrid cdi cdd stage full time job role team company
h f m w x d hf fh mw job jobs new grad experience experienced years ans
""".split())
_TOKEN_RE = re.compile(r"[a-zàâäéèêëîïôöùûüç0-9+#.]{3,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    words = [w.strip(".") for w in _TOKEN_RE.findall(text.lower())]
    words = [w for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit()]
    grams = set(words)
    grams.update(f"{a} {b}" for a, b in zip(words, words[1:]))
    return grams


def _doc_text(row) -> str:
    return f"{row['title']} {row['title']} {row['description'] or ''}"


def mine_rules(conn, min_support: int = 2, min_score: float = 0.34,
               max_rules: int = 25, persist: bool = True) -> dict:
    """Propose inactive filter rules from dismissed-vs-interested feedback.

    A term scores by (its share of dismissals) minus (its share of interests); a
    high positive score means "seen when you say no, not when you say yes".
    """
    neg = db.labeled_jobs(conn, "dismissed")
    pos = db.labeled_jobs(conn, "interested")
    if len(neg) < min_support:
        return {"status": "insufficient", "dismissed": len(neg), "interested": len(pos),
                "need": min_support, "suggested": 0, "new": 0, "rules": []}

    neg_df: Counter = Counter()
    for r in neg:
        neg_df.update(_tokens(_doc_text(r)))
    pos_df: Counter = Counter()
    for r in pos:
        pos_df.update(_tokens(_doc_text(r)))

    candidates: list[dict] = []
    for term, ndf in neg_df.items():
        if ndf < min_support:
            continue
        pdf = pos_df.get(term, 0)
        score = ndf / len(neg) - (pdf / len(pos) if pos else 0.0)
        if score < min_score:
            continue
        candidates.append({
            "kind": "negative_kw", "value": term,
            "score": round(score, 2), "neg_df": ndf, "pos_df": pdf,
            "evidence": f"{ndf}/{len(neg)} dismissed, {pdf}/{len(pos) or 0} interested",
        })

    comp = Counter(
        r["company"].strip().lower() for r in neg
        if r["company"] and "company" in (r["dismiss_reasons"] or "")
    )
    for company, n in comp.items():
        if n >= min_support:
            candidates.append({
                "kind": "company_block", "value": company,
                "score": round(n / len(neg), 2), "neg_df": n, "pos_df": 0,
                "evidence": f"dismissed {n}x with reason 'company'",
            })

    candidates.sort(key=lambda c: (c["neg_df"], c["score"]), reverse=True)
    candidates = candidates[:max_rules]

    new = 0
    if persist:
        for c in candidates:
            weight = 100 if c["kind"] == "company_block" else 20
            if db.add_rule(conn, c["kind"], c["value"], source="learned",
                           weight=weight, evidence=c["evidence"], active=0):
                new += 1

    return {"status": "ok", "dismissed": len(neg), "interested": len(pos),
            "suggested": len(candidates), "new": new, "rules": candidates}
