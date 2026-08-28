# Job Hunter — Paris ML-Engineer edition

Personal tool to fetch ML-engineer job postings (Paris / Île-de-France, CDI, no
internships), score them against your profile, auto-tailor a LaTeX CV and draft a
cover letter per job, and track every application from a local web dashboard.
**Draft-and-review by design: it never auto-submits applications.**

## Features
- **Fetch** from Welcome to the Jungle (public Algolia backend), company ATS
  boards (Greenhouse + Lever), and LinkedIn (public guest search, read-only, no
  login), filtered to France.
- **Score + filter** with a rule-based, junior-tuned matcher: title-based ML
  relevance gate, internships/alternance excluded, seniority as a soft penalty,
  Paris/IDF ranked above other-France.
- **LLM fit-judge** (Claude): 0–100 score + verdict + reasons, junior-calibrated.
- **LaTeX CV auto-tailoring**: reorders your projects/experience by relevance to
  each job, rewrites the "Seeking" tagline, compiles to PDF (`latexmk`).
- **Cover-letter drafting** (Claude): grounded in your real CV, matches the
  posting's language, never fabricates.
- **Track**: SQLite with cross-run dedup (by id *and* content); each posting moves
  through a status lifecycle from a table + kanban dashboard.

## Setup
```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
```
LLM features use whichever is available: `ANTHROPIC_API_KEY` (`pip install -e '.[llm]'`)
or the local `claude` CLI (subscription) — check with `jobhunter llm-status`.

## Use
```bash
P=.venv/bin/python
$P -m jobhunter.cli fetch            # fetch + score + store new jobs (WTTJ + ATS)
$P -m jobhunter.cli judge --min-score 40   # LLM-judge promising jobs
$P -m jobhunter.cli tailor <job_id>  # tailored CV -> data/cv/<job>/cv.pdf
$P -m jobhunter.cli cover  <job_id>  # cover letter -> data/cv/<job>/cover_letter.md
$P -m jobhunter.cli list  --min-score 40
$P -m jobhunter.cli web              # dashboard at http://127.0.0.1:8000
```

## Daily automation (cron)
`jobhunter run` is the scheduled entrypoint: fetch everywhere, then LLM-judge the
new promising jobs (capped per run). Manage the crontab entry with:
```bash
$P -m jobhunter.cli run                 # run once now (what cron calls)
$P -m jobhunter.cli cron show           # preview the crontab line (no changes)
$P -m jobhunter.cli cron install --time 08:00   # add the daily entry
$P -m jobhunter.cli cron uninstall      # remove it
```
Installing is opt-in because the daily run makes LLM calls. Only the tagged
`# jobhunter-daily` line is ever touched; your other cron jobs are preserved.
Output is appended to `data/cron.log`.

## Configuration
- `config/search.yaml` — query, role/boost keywords, geo, languages, exclude
  terms, company blocklist, `min_score`, `max_hits`.
- `config/companies.yaml` — ATS boards to pull (`name`, `ats`, `token`).
- `templates/cv_base.tex` — the base CV the tailoring reorders.

## Tests
```bash
.venv/bin/python -m pytest -q
```

## Layout
```
jobhunter/
  sources/wttj.py, sources/ats.py   fetch (WTTJ Algolia; Greenhouse/Lever)
  match.py                          rule-based junior-tuned scoring + relevance gate
  llm/provider.py                   Claude seam (API key or claude CLI)
  llm/judge.py, apply/cover_letter.py   LLM fit-judge + cover letter
  tailor/snippet_bank.py, tailor/engine.py   parse + reorder + compile CV
  db.py                             SQLite: jobs / applications / cv_artifacts
  pipeline.py                       fetch / judge / tailor / cover orchestration
  cli.py, web/                      CLI + FastAPI/Jinja dashboard (table + kanban)
config/, templates/, tests/, data/  (data/ created on first run; git-ignored)
```

## Roadmap
- Indeed / Glassdoor sources; LinkedIn logged-in enrichment (`li_at` cookie,
  secondary account) for descriptions.
- LLM-assisted screening-question answers with a cached Q→A store.
