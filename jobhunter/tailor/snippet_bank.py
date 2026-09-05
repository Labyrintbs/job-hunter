"""Parse the base LaTeX CV into reorderable blocks.

The base CV (Jake Gutierrez template) has a fixed structure: a centered heading
with a "Seeking ..." tagline, then EDUCATION, PROJECTS & RESEARCH, PROFESSIONAL
EXPERIENCE, and SKILLS. Projects are delimited by
`\\resumeProjectHeadingFourItemResearch` and experiences by `\\resumeSubheading`.
Skills are `\\textbf{Category:} ...` lines inside one \\item.

A retired entry (e.g. Data Joker, see templates/cv_tailoring_workflow.md) is
commented out with a leading '%' on every line rather than deleted, so it can be
revived for a closely-related job. The block splitter must strip full comment
lines before scanning for macro names, otherwise a plain substring search finds
the macro name sitting inside the comment and "resurrects" it as a live
invocation missing its (still-commented) argument braces -- which is exactly
what broke compilation before this fix.

engine.py selects a capped, relevance-ranked subset of projects/experiences/
skill categories (mirroring templates/cv_tailoring_workflow.md's rules: 2
experiences, 3 projects, reverse-chronological display order by default) rather
than just reordering everything, see MAX_EXPERIENCES/MAX_PROJECTS there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_MACRO = r"\resumeProjectHeadingFourItemResearch"
EXPERIENCE_MACRO = r"\resumeSubheading"
LIST_START = r"\resumeSubHeadingListStart"
LIST_END = r"\resumeSubHeadingListEnd"

SKILLS_BLOCK_RE = re.compile(r"(\\section\{SKILLS\}.*?\\item\{)(.*?)(\}\}\s*\\end\{itemize\})", re.S)
SKILL_LINE_RE = re.compile(r"\\textbf\{([^:]+):\}\s*(.*)")

# A block's own trailing date range, e.g. "{03/2026 -- 08/2026}" -- used to sort
# a selected subset reverse-chronologically regardless of relevance-rank order.
DATE_RANGE_RE = re.compile(r"(\d{2})/(\d{4})\s*--\s*(\d{2})/(\d{4})")

# Theme vocabulary — used to tag blocks and to match jobs to blocks.
VOCAB = {
    "llm", "language model", "nlp", "prompt", "transformer", "grpo", "sft",
    "fine-tun", "extraction", "reinforcement", "policy", "dialogue", "causal",
    "reward", "image", "vision", "cnn", "segmentation", "detection",
    "point cloud", "registration", "classification", "medical", "clinical",
    "ct", "cta", "imaging", "dice", "anatomy", "speech", "audio", "hubert",
    "wav2vec", "vocoder", "self-supervised", "deploy", "pipeline", "docker",
    "distributed", "ddp", "production", "annotation", "transfer learning",
    "data augmentation", "pytorch", "benchmark",
    "agent", "agentic", "retrieval", "embedding", "orchestration",
    "reranking", "vector database", "langgraph", "langchain",
}


@dataclass
class Block:
    text: str
    tags: set[str] = field(default_factory=set)

    def end_date(self) -> tuple[int, int]:
        """(year, month) of the block's own date range, for reverse-chronological
        sort; (0, 0) -- sorts last -- if no date range is found."""
        m = DATE_RANGE_RE.search(self.text)
        return (int(m.group(4)), int(m.group(3))) if m else (0, 0)

    def bullets(self) -> list[str]:
        """The block's own \\resumeItem{...} bullet contents, brace-matched (not a
        naive regex) so a bullet containing nested braces doesn't get cut short.
        Empty if this block has no bullet list (e.g. a description-only project
        like NeRF)."""
        return _bullet_contents(self.text)


def _brace_end(text: str, open_idx: int) -> int:
    """text[open_idx] must be '{'. Returns the index just past its matching '}'."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


_BULLET_START_RE = re.compile(r"\\resumeItem\{")


def _bullet_contents(text: str) -> list[str]:
    out = []
    for m in re.finditer(_BULLET_START_RE, text):
        end = _brace_end(text, m.end() - 1)
        out.append(text[m.end():end - 1])
    return out


def _split_bullet_list(text: str) -> tuple[str, list[str], str] | None:
    """Split `text` into (prefix incl. ListStart, [verbatim '\\resumeItem{...}'
    strings], suffix from ListEnd). None if there's no bullet list at all."""
    start = text.find(r"\resumeItemListStart")
    end = text.find(r"\resumeItemListEnd")
    if start == -1 or end == -1:
        return None
    body = text[start:end]
    bullets, pos = [], 0
    for m in re.finditer(_BULLET_START_RE, body):
        close = _brace_end(body, m.end() - 1)
        bullets.append(body[m.start():close])
        pos = close
    prefix = text[:start] + r"\resumeItemListStart"
    suffix = r"\resumeItemListEnd" + text[end + len(r"\resumeItemListEnd"):]
    return prefix, bullets, suffix


def filter_bullets(text: str, keep: list[int]) -> str:
    """Keep only the given bullet indices (0-based, in the block's own order).
    Reuse-only: never invents a bullet, only ever drops from what's already
    there. Falls back to the unfiltered block if there's no bullet list, or if
    `keep` would drop everything (never emit an empty itemize)."""
    split = _split_bullet_list(text)
    if split is None or not keep:
        return text
    prefix, bullets, suffix = split
    chosen = [bullets[i] for i in sorted(set(keep)) if isinstance(i, int) and 0 <= i < len(bullets)]
    if not chosen:
        return text
    return prefix + "\n        " + "\n        ".join(chosen) + "\n      " + suffix


@dataclass
class SkillCategory:
    name: str      # e.g. "Generative AI \& Agentic Systems"
    line: str      # full "\textbf{Name:} ..." line, no trailing '\\' or newline
    tags: set[str]


@dataclass
class ParsedCV:
    document: str          # full .tex source
    heading_line: str      # the "Seeking ..." tagline line (verbatim)
    projects: list[Block]
    experiences: list[Block]
    skills: list[SkillCategory]


def terms_in(text: str) -> set[str]:
    """Vocabulary terms present in text, matched on word boundaries so short
    tokens like 'ct' don't hit inside 'detection'/'structural'."""
    low = text.lower()
    found = set()
    for term in VOCAB:
        if re.search(r"\b" + re.escape(term), low):  # leading boundary + prefix (plurals, -ing)
            found.add(term)
    return found


def _tag(text: str) -> set[str]:
    return terms_in(text)


def _strip_comment_only_lines(text: str) -> str:
    """Drop lines that are pure LaTeX comments (first non-whitespace char '%'). A
    retired block's macro name is still findable by plain substring search inside
    such a line otherwise, and gets resurrected as a live invocation, see the
    module docstring."""
    return "\n".join(line for line in text.split("\n") if not re.match(r"^\s*%", line))


def _split_items(section_body: str, macro: str) -> list[str]:
    """Split a section body (between ListStart/ListEnd) into per-item blocks."""
    section_body = _strip_comment_only_lines(section_body)
    idx = section_body.find(macro)
    if idx == -1:
        return []
    chunk = section_body[idx:]
    parts = chunk.split(macro)
    return [macro + p for p in parts[1:]]


def _section_body(document: str, title_regex: str) -> tuple[int, int, str] | None:
    """Return (start, end, body) of the ListStart..ListEnd span inside a section."""
    m = re.search(r"\\section\{" + title_regex + r"\}", document)
    if not m:
        return None
    start = document.find(LIST_START, m.end())
    end = document.find(LIST_END, start)
    if start == -1 or end == -1:
        return None
    return start + len(LIST_START), end, document[start + len(LIST_START):end]


def _parse_skills(doc: str) -> list[SkillCategory]:
    m = SKILLS_BLOCK_RE.search(doc)
    if not m:
        return []
    cats = []
    for raw in _strip_comment_only_lines(m.group(2)).split("\n"):
        line = raw.strip()
        if line.endswith(r"\\"):
            line = line[:-2].rstrip()
        if not line.startswith(r"\textbf{"):
            continue
        lm = SKILL_LINE_RE.match(line)
        if not lm:
            continue
        cats.append(SkillCategory(name=lm.group(1), line=line, tags=_tag(line)))
    return cats


def parse(base_path: Path) -> ParsedCV:
    doc = base_path.read_text(encoding="utf-8")

    heading = ""
    hm = re.search(r"\{Seeking[^\n]*\}", doc)
    if hm:
        heading = hm.group(0)

    projects: list[Block] = []
    proj = _section_body(doc, r"PROJECTS[^}]*")
    if proj:
        projects = [Block(t, _tag(t)) for t in _split_items(proj[2], PROJECT_MACRO)]

    experiences: list[Block] = []
    exp = _section_body(doc, r"PROFESSIONAL EXPERIENCE")
    if exp:
        experiences = [Block(t, _tag(t)) for t in _split_items(exp[2], EXPERIENCE_MACRO)]

    return ParsedCV(document=doc, heading_line=heading, projects=projects,
                     experiences=experiences, skills=_parse_skills(doc))


def reassemble(doc: str, section_title_regex: str, ordered_items: list[Block]) -> str:
    """Replace a section's item span with the reordered items."""
    span = _section_body(doc, section_title_regex)
    if not span:
        return doc
    start, end, _ = span
    new_body = "\n" + "\n".join(b.text.rstrip() for b in ordered_items) + "\n    "
    return doc[:start] + new_body + doc[end:]


def reassemble_skills(doc: str, categories: list[SkillCategory]) -> str:
    """Replace the SKILLS item lines with a filtered subset, keeping every
    existing line verbatim (reuse-only -- never invents an item)."""
    m = SKILLS_BLOCK_RE.search(doc)
    if not m or not categories:
        return doc
    suffix = "\\\\"
    lines = [f"    {c.line}{suffix if i < len(categories) - 1 else ''}"
              for i, c in enumerate(categories)]
    inner = "\n" + "\n".join(lines) + "\n    "
    start, end = m.start(2), m.end(2)
    return doc[:start] + inner + doc[end:]
