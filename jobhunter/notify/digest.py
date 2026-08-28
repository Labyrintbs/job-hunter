"""Format a digest of new matching jobs for notification."""
from __future__ import annotations


def effective_score(row: dict) -> int:
    """Prefer the LLM fit score when available, else the rule score."""
    llm = row.get("llm_score")
    return int(llm) if llm is not None else int(row.get("score", 0))


def select(rows: list[dict], min_score: int) -> list[dict]:
    kept = [r for r in rows if effective_score(r) >= min_score]
    return sorted(kept, key=effective_score, reverse=True)


def _line(row: dict) -> str:
    s = effective_score(row)
    verdict = f" ({row['llm_verdict']})" if row.get("llm_verdict") else ""
    loc = row.get("location", "")
    return f"[{s:3d}]{verdict} {row['title']} — {row['company']} · {loc}\n{row.get('url','')}"


def subject(rows: list[dict]) -> str:
    n = len(rows)
    return f"Job Hunter: {n} new matching job{'s' if n != 1 else ''}"


def text(rows: list[dict]) -> str:
    if not rows:
        return "No new matching jobs."
    return "\n\n".join(_line(r) for r in rows)


def markdown(rows: list[dict]) -> str:
    lines = [f"# {subject(rows)}", ""]
    for r in rows:
        s = effective_score(r)
        verdict = f" _{r['llm_verdict']}_" if r.get("llm_verdict") else ""
        title = f"[{r['title']}]({r['url']})" if r.get("url") else r["title"]
        lines.append(f"- **{s}**{verdict} — {title} — {r['company']} · {r.get('location','')}")
        if r.get("llm_reasons"):
            lines.append(f"    - {r['llm_reasons']}")
    return "\n".join(lines) + "\n"
