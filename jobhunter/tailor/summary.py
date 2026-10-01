"""The CV summary: two to three sentences written by the LLM for one job, from the
full master CV and the posting. The variant for the job's role category is only
the style and content anchor shown to the LLM, and the fallback when its text
fails validation twice.

Validation rejects anything that adds a number, tool or name that neither the CV
nor the posting's title and company contain.
"""
from __future__ import annotations

import html
import re
from typing import NamedTuple

import yaml

from ..config import REPO_ROOT
from ..lang import counts
from ..llm import provider
from ..models import Job

VARIANTS_PATH = REPO_ROOT / "templates" / "summary_variants.yaml"

MIN_WORDS, MAX_WORDS, MAX_SENTENCES = 35, 58, 3

_NOISE_COMPANIES = {"non renseigné", "non renseigne", "confidentiel", "confidential",
                    "anonymous", "stealth", "n/a", "unknown"}
_TITLE_NOISE = re.compile(
    r"\(?\b(?:[hf]\s*/\s*[fh](?:\s*/\s*nb)?|[fmw]\s*/\s*[fmw]\s*/\s*[dxn])\b\)?", re.I)
_CONTRACT_TAG = re.compile(
    r"^\s*(?:CDI|CDD|Stage|Alternance|Freelance|Full[- ]time|Part[- ]time|Internship)\s*[-–—:|]\s*", re.I)
_ROLE_WORD = re.compile(
    r"\b(?:engineer|scientist|developer|developpeur|développeur|manager|analyst|researcher|"
    r"research|consultant|architect|ingénieur|ingenieur|chercheur|lead|specialist|expert|"
    r"officer|programmer)\b", re.I)
# Per summary language. "one"/"un" are left out of the spelled numbers on purpose:
# "one of the", "un poste" are ordinary prose.
_FIRST_PERSON = {
    "en": re.compile(r"\b(?:i|my|me|mine|we|our)\b", re.I),
    "fr": re.compile(r"\b(?:je|j'|mon|ma|mes|moi|nous|notre|nos)\b|\bj'", re.I),
}
_HYPE = {
    "en": re.compile(r"\b(?:passionate|excited|thrilled|motivated|dream|eager|enthusiastic)\b", re.I),
    "fr": re.compile(r"\b(?:passionné\w*|enthousiaste\w*|motivé\w*|ravi\w*|rêve|impatient\w*)\b", re.I),
}
_SPELLED_NUMBER = {
    "en": re.compile(r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
                     r"thirty|fifty|hundred|thousand|million|dozen)\b", re.I),
    "fr": re.compile(r"\b(?:deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|vingt|"
                     r"trente|cinquante|cent|mille|million|douzaine)\b", re.I),
}
MAX_WORDS_FR = 64          # French runs a few words longer for the same content
LANGUAGE_REASON = "tailor_language_mismatch"
_DASHES = re.compile(r"[—–]| - ")
# A standalone figure ("40.8%", "17"); names like "3D" or "Qwen3.5-4B" don't match.
_FIGURE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?%?(?![\w])")
_TOKEN = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9+#./-]*")
# Common tools the CV does not claim; caught even when written in lower case.
_UNCLAIMED_TOOLS = (
    "kubernetes", "tensorflow", "spark", "hadoop", "aws", "azure", "gcp", "sql",
    "java", "scala", "airflow", "terraform", "react", "kafka", "snowflake",
    "databricks", "mlflow", "sagemaker", "golang", "rust", "javascript", "typescript",
)


class Summary(NamedTuple):
    text: str
    reason: str = ""    # fetch_diag reason when the LLM text was not used
    detail: str = ""


def load_variants(path=VARIANTS_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def pick_core(role_category: str, variants: dict | None = None, lang: str = "en") -> str:
    variants = variants or load_variants()
    key = "core_fr" if lang == "fr" else "core"
    for v in variants["variants"].values():
        if role_category and role_category in (v.get("categories") or []):
            return " ".join(v[key].split())
    return " ".join(variants["variants"]["general"][key].split())


def clean_title(title: str) -> str:
    """The core role name: gender marks and a leading contract tag are dropped,
    then the title is split at separators (" - ", " | ", a colon, a parenthesis)
    and the first part that names a role (Engineer, Scientist...) is kept."""
    t = _CONTRACT_TAG.sub("", _TITLE_NOISE.sub(" ", html.unescape(title or "")))
    parts = [re.sub(r"\s+", " ", p).strip(" -–—,:;|/")
             for p in re.split(r"\s+[-–—|]\s+|\s*[:(|]", t)]
    parts = [p for p in parts if p]
    pick = next((p for p in parts if _ROLE_WORD.search(p)), parts[0] if parts else "")
    pick = re.sub(r"\bai\b", "AI", pick)
    return pick if 2 <= len(pick) <= 60 else ""


def clean_company(company: str) -> str:
    c = html.unescape(company or "").strip()
    if not (2 <= len(c) <= 40) or c.lower() in _NOISE_COMPANIES:
        return ""
    return c


def closing_line(title: str, company: str, lang: str = "en") -> str:
    t, c = clean_title(title), clean_company(company)
    if lang == "fr":
        if t and c:
            return f"Souhaite mettre cette expérience au service du poste de {t} chez {c}."
        if t:
            return f"Souhaite mettre cette expérience au service du poste de {t}."
        if c:
            return f"Souhaite mettre cette expérience au service d'un poste chez {c}."
        return ""
    if t and c:
        return f"Looking to bring this to the {t} role at {c}."
    if t:
        return f"Looking to bring this to the {t} role."
    if c:
        return f"Looking to bring this to a role at {c}."
    return ""


def latex_escape(text: str) -> str:
    out = []
    for ch in text:
        out.append({"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                    "^": r"\textasciicircum{}", "\\": r"\textbackslash{}"}.get(ch, ch))
    return "".join(out)


def _has(word: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def language_problem(text: str, lang: str) -> str:
    """"" when `text` is in `lang`, else why not. Only function words count, so
    English technical terms inside a French summary are fine."""
    fr_hits, en_hits = counts(text)
    if lang == "fr" and en_hits > 1:
        return f"language: {en_hits} English function words in a French text"
    if lang == "en" and fr_hits > 2:
        return f"language: {fr_hits} French function words in an English text"
    return ""


def validate_summary(text: object, allowed_text: str, company: str = "", lang: str = "en") -> str:
    """"" when the summary is acceptable, else a short reason. `allowed_text` is
    everything it may mention: the CV, the job title and the company. A reason
    starting "language" is a wrong-language text (see LANGUAGE_REASON)."""
    if not isinstance(text, str) or not text.strip():
        return "empty"
    s = text.strip()
    if "\n" in s:
        return "multi-line"
    wrong_language = language_problem(s, lang)
    if wrong_language:
        return wrong_language
    n = len(s.split())
    if not MIN_WORDS <= n <= (MAX_WORDS_FR if lang == "fr" else MAX_WORDS):
        return f"{n} words"
    if len(re.findall(r"[.!?](?:\s|$)", s)) > MAX_SENTENCES:
        return "too many sentences"
    if _SPELLED_NUMBER[lang].search(s):
        return "contains a number"
    if _FIGURE.search(s):
        return "contains a figure"
    if _FIRST_PERSON[lang].search(s):
        return "first person"
    if _HYPE[lang].search(s):
        return "hype word"
    if _DASHES.search(s):
        return "dash"
    allowed, low = allowed_text.lower(), s.lower()
    for tool in _UNCLAIMED_TOOLS:
        if _has(tool, low) and not _has(tool, allowed):
            return f"unsupported term {tool}"
    # any real word of the company name counts ("Terabase" for "TERABASE ENERGY INC")
    words = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", company)]
    if words and not any(_has(w, low) for w in words):
        return "company not named"
    for m in _TOKEN.finditer(s):
        tok = m.group(0)
        t = tok.lower().strip(".,/-")
        has_digit = any(c.isdigit() for c in tok)
        # a capitalised sentence opener ("Targeting", "Now") is ordinary prose, not a name
        opener = not s[:m.start()].strip() or s[:m.start()].rstrip()[-1] in ".!?"
        notable = any(c.isupper() for c in tok[1:]) or has_digit or (tok[0].isupper() and not opener)
        if has_digit and not _has(t, allowed):
            return f"unsupported number {tok}"
        # "LLMs" for "LLM": a plural is the same term
        if notable and t and not re.search(r"\b" + re.escape(t.rstrip("s")), allowed):
            return f"unsupported term {tok}"
    return ""


_LANGUAGE_RULE = {
    "en": "plain professional English",
    "fr": ("polished professional FRENCH (the whole summary in French, with French punctuation; "
           "keep only standard technical terms in English, as French ML job posts do, e.g. "
           "fine-tuning, LLM-as-a-judge, pipeline, prompt, workflow; no English function words "
           "such as the, with, and, for)"),
}

_SYSTEM = (
    "You write the summary paragraph at the top of a candidate's CV, tailored to one job "
    "posting. Write 2 or 3 natural, complete sentences (40 to 50 words in total) in {language}. "
    "Open with who the candidate is, in the posting's own role "
    "vocabulary where the CV honestly supports it; then the experience from the CV that is "
    "most relevant to what this posting actually asks for; then, naturally, name the target "
    "role and company once. Every claim must come from the CV text given: never add a tool, "
    "employer, number, degree or achievement that is not there, never stretch a claim to fit "
    "the posting, and skip CV material that is not relevant to it. No first person, no dashes "
    "as connectors, no words like passionate, excited or motivated, no figures, percentages or "
    "counts (names such as 3D are fine). Do not repeat the CV's bullet wording line by "
    "line; write it as one fluent paragraph."
)

_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def generate_summary(job: Job, anchor: str, title: str, company: str, feedback: str = "",
                     lang: str = "en") -> str | None:
    """LLM-written summary text, or None when no backend is available."""
    if not provider.available():
        return None
    from ..llm.profile import profile_text  # profile imports tailor.engine, which imports this module
    if lang == "fr":
        target = " ".join(x for x in (f"le poste de {title}" if title else "",
                                      f"chez {company}" if company else "") if x)
    else:
        target = " ".join(x for x in (f"the {title} role" if title else "",
                                      f"at {company}" if company else "") if x)
    prompt = (
        f"CANDIDATE CV ({'French' if lang == 'fr' else 'English'} version):\n{profile_text(lang)}\n\n"
        f"EXAMPLE OF THE INTENDED CONTENT AND TONE (for this kind of role; adapt it, do not copy it):\n{anchor}\n\n"
        f"JOB POSTING:\nTitle: {job.title}\nCompany: {job.company}\n"
        f"Description:\n{(job.description or '')[:6000]}\n\n"
        + (f"Name the company once, and refer to the role by its core job title, not the whole "
           f"posting title (for example: {target}).\n" if target else "")
        + (f"\nYour previous attempt was rejected: {feedback}. Fix that.\n" if feedback else "")
    )
    system = _SYSTEM.format(language=_LANGUAGE_RULE[lang])
    return provider.generate_json(prompt, system=system, max_tokens=500, json_schema=_SCHEMA).get("summary")


def build(job: Job, role_category: str, cv_text: str, lang: str = "en") -> Summary:
    """The summary text (plain, not yet LaTeX-escaped) for this job, in `lang`."""
    anchor = pick_core(role_category, lang=lang)
    title, company = clean_title(job.title), clean_company(job.company)
    fallback = f"{anchor} {closing_line(job.title, job.company, lang)}".strip()
    allowed = f"{cv_text} {job.title} {job.company}"

    feedback = reason = detail = ""
    for _ in range(2):
        try:
            text = generate_summary(job, anchor, title, company, feedback, lang=lang)
        except Exception as exc:
            return Summary(fallback, "tailor_summary_llm_error", f"{type(exc).__name__}: {exc}"[:200])
        if text is None:
            return Summary(fallback)    # the selection call already tracks "no LLM backend"
        why = validate_summary(text, allowed, company, lang)
        if not why:
            return Summary(text.strip())
        wrong_language = why.startswith("language")
        feedback = (f"{why}. Write the whole summary in {'French' if lang == 'fr' else 'English'}, "
                    f"keeping only standard technical terms in the other language") if wrong_language else why
        reason, detail = (LANGUAGE_REASON if wrong_language else "tailor_summary_rejected"), why
    return Summary(fallback, reason, detail)
