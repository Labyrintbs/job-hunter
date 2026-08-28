"""Parse the base LaTeX CV into reorderable blocks.

The base CV (Jake Gutierrez template) has a fixed structure: a centered heading
with a "Seeking ..." tagline, then EDUCATION, PROJECTS & RESEARCH, PROFESSIONAL
EXPERIENCE, and SKILLS. Projects are delimited by
`\\resumeProjectHeadingFourItemResearch` and experiences by `\\resumeSubheading`.

We keep every block (a one-page CV shows all of it) but reorder projects and
experiences by relevance to a given job, and rewrite the tagline. That is the
deterministic "modular + auto-generate" tailoring; an LLM bullet-rewrite layer
can slot in on top later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_MACRO = r"\resumeProjectHeadingFourItemResearch"
EXPERIENCE_MACRO = r"\resumeSubheading"
LIST_START = r"\resumeSubHeadingListStart"
LIST_END = r"\resumeSubHeadingListEnd"

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
}


@dataclass
class Block:
    text: str
    tags: set[str] = field(default_factory=set)


@dataclass
class ParsedCV:
    document: str          # full .tex source
    heading_line: str      # the "Seeking ..." tagline line (verbatim)
    projects: list[Block]
    experiences: list[Block]


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


def _split_items(section_body: str, macro: str) -> list[str]:
    """Split a section body (between ListStart/ListEnd) into per-item blocks."""
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

    return ParsedCV(document=doc, heading_line=heading, projects=projects, experiences=experiences)


def reassemble(doc: str, section_title_regex: str, ordered_items: list[Block]) -> str:
    """Replace a section's item span with the reordered items."""
    span = _section_body(doc, section_title_regex)
    if not span:
        return doc
    start, end, _ = span
    new_body = "\n" + "\n".join(b.text.rstrip() for b in ordered_items) + "\n    "
    return doc[:start] + new_body + doc[end:]
