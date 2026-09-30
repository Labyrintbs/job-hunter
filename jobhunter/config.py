from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "jobhunter.db"
CONFIG_PATH = REPO_ROOT / "config" / "search.yaml"
SCORING_PATH = REPO_ROOT / "config" / "scoring.yaml"
COMPANIES_PATH = REPO_ROOT / "config" / "companies.yaml"
JUDGE_PREFERENCES_PATH = REPO_ROOT / "config" / "judge_preferences.yaml"


def load_search_config(path: Path | None = None) -> dict:
    """search.yaml (fetch sources, geo/language/exclude-term filter lists) merged
    with scoring.yaml (every point value match.py's score()/screen() uses) -- one
    dict, so every existing caller keeps working unchanged regardless of which
    file a given key actually lives in."""
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    with open(SCORING_PATH, "r", encoding="utf-8") as f:
        config.update(yaml.safe_load(f))
    return config


def load_companies(path: Path | None = None) -> list[dict]:
    path = path or COMPANIES_PATH
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("companies", [])


def add_company(name: str, ats: str, token: str, path: Path | None = None) -> bool:
    """Append one ATS entry to companies.yaml as text, so the file's comments and
    layout survive (a yaml dump would drop them). False if the name is already there."""
    path = path or COMPANIES_PATH
    if name.strip().lower() in {c["name"].strip().lower() for c in load_companies(path)}:
        return False
    text = path.read_text(encoding="utf-8")
    prefix = "" if text.endswith("\n") else "\n"
    entry = f"  - {{ name: {json.dumps(name, ensure_ascii=False)}, ats: {ats}, token: {token} }}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + entry)
    return True


def load_standing_preferences(path: Path | None = None) -> list[str]:
    path = path or JUDGE_PREFERENCES_PATH
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("standing_preferences") or []
