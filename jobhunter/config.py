from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "jobhunter.db"
CONFIG_PATH = REPO_ROOT / "config" / "search.yaml"
COMPANIES_PATH = REPO_ROOT / "config" / "companies.yaml"


def load_search_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_companies(path: Path | None = None) -> list[dict]:
    path = path or COMPANIES_PATH
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("companies", [])
