# Job Hunter — Paris ML-Engineer edition

Personal tool to fetch ML-engineer job postings (Paris / Île-de-France, CDI, no
internships), score them against a profile, track applications, and — in later
phases — auto-tailor a LaTeX CV per job. **Draft-and-review by design: it never
auto-submits applications.**

## Status (MVP)
Working end-to-end today:
- **Fetch** from Welcome to the Jungle (public Algolia backend, no login).
- **Score + filter** with a rule-based, junior-tuned matcher (Paris/IDF ranked
  above other-France; internships/alternance excluded; seniority = soft penalty).
- **Store** in SQLite with cross-run dedup (by id *and* content — WTTJ reposts the
  same job under several ids).
- **Track** each posting through a status lifecycle from a local web dashboard
  (table + kanban), with a one-click "Fetch new jobs" button.

Seams are in place for the next phases: LinkedIn/Indeed/ATS sources, an LLM judge,
and LaTeX CV auto-tailoring (`cv_artifacts` table + `data/cv/` already wired).

## Setup
```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m playwright install chromium   # for later LinkedIn phase
```

## Use
```bash
.venv/bin/python -m jobhunter.cli fetch     # fetch + score + store new jobs
.venv/bin/python -m jobhunter.cli list      # print stored jobs (--status, --min-score)
.venv/bin/python -m jobhunter.cli web       # dashboard at http://127.0.0.1:8000
```

## Daily automation (cron)
```
0 8 * * *  cd /Users/hongmingfang/Documents/job-hunter && .venv/bin/python -m jobhunter.cli fetch
```

## Configuration
Edit `config/search.yaml` — query, boost keywords, geo, languages, exclude terms
(internships etc.), company blocklist, `min_score`, `max_hits`.

## Layout
```
jobhunter/
  sources/wttj.py   WTTJ Algolia fetch (verified live)
  match.py          rule-based junior-tuned scoring
  db.py             SQLite: jobs / applications / cv_artifacts + dedup
  pipeline.py       fetch -> score -> store
  cli.py            fetch / list / web / init
  web/              FastAPI + Jinja dashboard (table + kanban)
config/search.yaml
data/jobhunter.db   (created on first run)
```
