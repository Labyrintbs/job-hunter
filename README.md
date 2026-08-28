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
  relevance gate, internships/alternance excluded, Paris/IDF ranked above
  other-France.
- **Seniority screen + Filtered bucket**: obvious senior/lead/staff/confirmé
  titles and postings requiring more than `seniority.max_years` are auto-hidden
  into a Filtered bucket (never deleted — reviewable and restorable). A
  junior/new-grad title always overrides the gate; below-`min_score` jobs land
  there too. `jobhunter list --filtered` or the 🕳 filtered pill in the dashboard.
- **Lazy enrichment**: LinkedIn guest cards carry no description; for jobs you've
  engaged with (marked interested, or moved past `new`) the full text is fetched
  on demand — LinkedIn via its guest detail endpoint, others via the job page —
  and written back so the judge and learning phases have real content.
  CLI: `jobhunter enrich [<id>]`; runs automatically (bounded) in `jobhunter run`.
- **LLM fit-judge** (Claude): 0–100 score + verdict + reasons + seniority read
  (`seniority`, `min_years_required`), junior-calibrated.
- **LaTeX CV auto-tailoring**: reorders your projects/experience by relevance to
  each job, rewrites the "Seeking" tagline, compiles to PDF (`latexmk`).
- **Cover-letter drafting** (Claude): grounded in your real CV, matches the
  posting's language, never fabricates.
- **Feedback (explicit-only)**: 👍 interested / 👎 dismiss with fixed reason chips
  (`too_senior`, `wrong_domain`, `location`, …). Dismissed jobs leave the main list
  into a Dismissed view; 👍 rescues a job from the Filtered bucket. This is the
  clean ground-truth signal the learning phases (4/5) will mine. CLI:
  `jobhunter feedback <id> --label dismissed --reasons too_senior,location`.
- **Learn filter rules (approval-gated)**: a transparent miner reads your dismissed
  (negatives) vs interested (positives) jobs and proposes discriminative keyword /
  company rules by document-frequency difference — each with its evidence. Rules
  land **inactive**; nothing filters your jobs until you approve it in the Rules
  page (or `jobhunter rules approve <id>`). CLI: `jobhunter rules mine|list|approve|reject|add`.
- **Preference profile (LLM-condensed)**: Claude distills your interested-vs-dismissed
  jobs into a short "Prefer … / Avoid …" instruction block, versioned in the DB and
  shown on the Rules page. It's injected into the LLM judge (Phase 5). Grounded in
  your evidence only. CLI: `jobhunter profile show|update`.
- **Track**: SQLite with cross-run dedup (by id *and* content); each posting moves
  through a status lifecycle from a table + kanban dashboard.
- **Market trends**: every fetch records an aggregate snapshot (`fetch_runs`) and
  stamps each job's `last_seen`; jobs carry a `geo_tier` (idf/france/remote/outside).
  Grafana/Metabase-ready SQL views + `jobhunter export` (CSV/JSON). See *Market trends* below.
- **Notify**: after each scheduled run, a digest of new high-fit jobs goes to the
  configured channels — File (always on), Telegram, or Email.

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

## Market trends (Grafana / Metabase)
Trend data accumulates automatically: each `fetch`/`run` inserts a `fetch_runs` row
and refreshes every job's `last_seen`. Four SQL views are the query surface:
`v_new_jobs_by_day` (volume + IDF share via `geo_tier`), `v_market_by_run`,
`v_top_companies`, `v_score_seniority_mix`.

SQLite stays the source of truth — point a BI tool at it:
- **Metabase** reads `data/jobhunter.db` natively → query the views directly.
- **Grafana** needs the `frser-sqlite-datasource` plugin for direct SQLite, **or** the
  no-plugin path: run `jobhunter web` and add a CSV/Infinity datasource at
  `http://127.0.0.1:8000/export/<view>.csv`.
- **Files**: `jobhunter export [--view <name>] [--format csv|json] [--out DIR]` dumps
  one file per view (default `data/export/`).

## Configuration
- `config/search.yaml` — query, role/boost keywords, geo, languages, exclude
  terms, company blocklist, `min_score`, `max_hits`, `linkedin`, `notifications`.
- `config/companies.yaml` — ATS boards to pull (`name`, `ats`, `token`).
- `templates/cv_base.tex` — the base CV the tailoring reorders.

### Notifications
Set `notifications.channels` in `search.yaml` (e.g. `[file, telegram]`) and provide
credentials via environment:
```bash
# Telegram
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
# Email (SMTP)
export SMTP_HOST=smtp.example.com SMTP_PORT=587 SMTP_USER=you@example.com \
       SMTP_PASSWORD=... EMAIL_TO=you@example.com
```
Test any time: `jobhunter notify --min-score 60` (File channel needs no setup and
writes `data/notifications/`).

## Tests
```bash
.venv/bin/python -m pytest -q
```

## Layout
```
jobhunter/
  sources/wttj.py, sources/ats.py   fetch (WTTJ Algolia; Greenhouse/Lever)
  match.py                          scoring + relevance/seniority gate; applies approved rules
  enrich.py                         lazy full-description fetch for engaged jobs
  learn.py                          rule miner + LLM preference-profile condenser
  llm/provider.py                   Claude seam (API key or claude CLI)
  llm/judge.py, apply/cover_letter.py   LLM fit-judge (profile-injected) + cover letter
  tailor/snippet_bank.py, tailor/engine.py   parse + reorder + compile CV
  db.py                             SQLite: jobs / applications / cv_artifacts / filter_rules / preference_profile
  pipeline.py                       fetch / enrich / judge / tailor / cover orchestration
  cli.py, web/                      CLI + FastAPI/Jinja dashboard (table + kanban + rules)
config/, templates/, tests/, data/  (data/ created on first run; git-ignored)
```

## The feedback loop (how filtering sharpens over time)
1. **Screen** — obvious senior/mismatch jobs auto-hide into the Filtered bucket.
2. **Judge** you — 👍/👎 with reasons; explicit-only ground truth.
3. **Enrich** — engaged jobs get their full description fetched.
4. **Learn** — `rules mine` proposes keyword/company rules (approval-gated);
   `profile update` distills a Prefer/Avoid block.
5. **Apply** — approved rules feed `match.screen`; the profile feeds the LLM judge.
6. **Calibrate** — `jobhunter metrics` reports the false-negative rate (interested
   jobs the screen had hidden); if it climbs, loosen `seniority.max_years` or drop a rule.

## Roadmap
- Indeed / Glassdoor sources; LinkedIn logged-in enrichment (`li_at` cookie,
  secondary account) for descriptions.
- LLM-assisted screening-question answers with a cached Q→A store.
- Auto-suggest a `seniority.max_years` bump when the false-negative rate is high.
- Weekly digest of rule hit-counts + profile drift.
