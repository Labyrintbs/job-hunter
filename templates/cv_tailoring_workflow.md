# CV Tailoring Workflow

Normal use is a Claude Code session running inside the `job-hunter` repo, with a job description pasted in (from LinkedIn/WTTJ/wherever, or by job_id if it's already in the DB). If starting fresh elsewhere without repo access, attach the master CV `.tex` (`templates/cv_base.tex`) and paste the job description instead, and skip the DB steps below.

---

## Context

I'm Hongming Fang, finishing an MSc in Computer Science (Image specialization) at Sorbonne University in September 2026. I have a CTI-accredited French engineering degree from École Centrale de Pékin / Beihang University. I'm looking for a full-time CDI in ML/NLP/LLM engineering in Île-de-France, starting September 2026.

Visa constraint: I need a CDI with gross annual base salary above the Passeport Talent "salarié qualifié" threshold (€39,582 for 2026). This is a floor, not a target — market rate for junior ML roles in Paris is €45,000–55,000.

Two things I need from you on a JD: an honest assessment of whether the role is worth applying to, and if it is, a tailored version of the CV.

## Step 0: Use the DB, don't rely on conversation memory alone

This repo tracks every posting in a local sqlite DB (`jobhunter` package, `db.py`). Query it directly rather than trusting only what's been said earlier in the conversation, memory doesn't survive a fresh session, the DB does.

**Before evaluating (feeds Step 1's duplicate check and gives a cross-check on the verdict):**

```python
from jobhunter import db
with db.connect() as conn:
    cur = conn.execute("select id, title, company, score, seniority, min_years, status "
                        "from jobs j join applications a on a.job_id=j.id "
                        "where j.company like '%<name>%'")
    for r in cur.fetchall(): print(dict(r))
```

If the posting is already there, its rule-based `score`/`seniority`/`min_years` is a cross-check, not the verdict itself, a high score doesn't override a qualitative red flag (company scale, RAG-as-core-requirement, etc.), but a mismatch between the rule score and my read is worth calling out.

**Tailoring should start from the real job_id, not a hand-typed copy:**

```bash
PATH="/Library/TeX/texbin:$PATH" python -m jobhunter.cli tailor <job_id>
```

This looks the job up in the DB, generates `data/cv/<job_id>-<company-slug>/cv.tex`, compiles a baseline PDF, records the artifact, and marks the job `cv_ready` automatically. Hand-edit from that baseline per Step 2 below, don't retype the master from scratch.

**After I say I've submitted an application, update its status, don't wait to be asked separately:**

```python
from jobhunter import db
with db.connect() as conn:
    db.update_status(conn, <job_id>, 'applied')
    conn.commit()
```

Valid statuses: `new`, `shortlisted`, `cv_ready`, `applied`, `responded`, `interview`, `offer`, `rejected`, `unavailable`.

**If a job isn't in the DB at all** (applied directly outside the fetch pipeline, e.g. a rejection email surfaces one I never tracked), back-fill it rather than leaving it untracked: `db.upsert_job(...)` with `source='manual'` and a made-up but unique `external_id`, then `db.update_status(...)`. Put any context (how it was found, a rejection email's content) in `applications.notes` via a raw `UPDATE applications SET notes = ? WHERE job_id = ?`.

These are local writes to my own tracking DB, not external actions, no need to ask permission before running them.

## Step 0.5: Environment (compiling & rendering)

Full detail and known-breakage notes: `CLAUDE.md` at the repo root (auto-loaded each session). Summary:

```bash
# Compile LaTeX -> PDF (latexmk/pdflatex aren't on PATH for the Bash tool)
PATH="/Library/TeX/texbin:$PATH" latexmk -pdf -g -interaction=nonstopmode cv.tex

# Render PDF pages -> PNG to actually look at them (pdftoppm isn't on PATH either,
# and the Homebrew ghostscript/poppler install is broken as of 2026-09-03)
DYLD_LIBRARY_PATH="/Users/tuboshu/opt/anaconda3/envs/dalas/lib" \
  /Users/tuboshu/opt/anaconda3/envs/dalas/bin/pdftoppm -png -r 100 cv.pdf page
```

Use `-g` to force a real rebuild, `latexmk` sometimes reports "up-to-date" without recompiling despite a source change. Delete the rendered PNGs after checking them, they're scratch output.

## Step 1: Evaluate the job before tailoring anything

Don't start editing until we've agreed the role is worth it. Give me a direct verdict, invest or pass, with reasoning on:

**Company type.** I'm targeting product companies (SaaS B2B, LegalTech, FinTech, HealthTech), 10–500 people. I want to avoid ESNs, staffing firms, and pure consultancies where I'd be placed on client "missions." Tells: the words *consultant*, *mission*, *chez nos clients*, *accompagner nos clients*, "cabinet de conseil," headcount in the thousands with offices in 20+ countries. Note that some consultancies have genuinely strong technical roles, flag those as a judgment call rather than an automatic pass, since I've decided case by case.

**Seniority.** Titles with Staff / Principal / Lead / Head of are still worth flagging, and note any explicit team-leadership expectations, not just the years-figure. But treat a bare years-of-experience gap as a minor consideration, not an automatic pass trigger: if I otherwise fit most of the role's actual requirements (technical direction, domain, other flags all clear), say the gap exists and let me weigh it, don't let it alone decide the verdict. Roles that explicitly count internships or say "jeune diplômé" are still the sweet spot, but their absence isn't disqualifying by itself anymore.

**Technical direction.** My work is LLM/NLP model development: fine-tuning, agentic orchestration, evaluation design. Roles that are actually MLOps/infrastructure (Kubernetes, Airflow, Terraform, CI/CD, Spark/Kafka, feature stores, data warehouses) or Data Engineering (ETL, dbt, BigQuery, Snowflake) are a different career track, say so directly. Quick heuristic: if more than half the listed skills are infrastructure tooling, it's not my direction. Full-stack roles requiring TypeScript/React/Node are also out.

**Citizenship / eligibility — hard red line, never weighed against fit.** If a posting states citizenship in a specific country as an eligibility requirement (not just "security clearance needed" or "must be clearable", an explicit citizenship bar, e.g. "must hold citizenship in the target territory"), that's an automatic Pass regardless of how good the rest of the fit is. I'm a Chinese national; I don't hold French or other target-country citizenship. Unlike the Seniority relaxation above, do not soften this one, don't let strong technical fit or a great company pull it back into "worth discussing." Scan the full posting for this before writing the verdict, it's often buried near the bottom under a "Security & Compliance" or similar heading, not in the main requirements list. Defense/dual-use work more broadly (without an explicit citizenship bar, just clearance-flavored language) is a softer version of the same problem and still worth flagging, but treat the explicit-citizenship case as categorically harder than that.

**Other flags.** Salary below the visa threshold is disqualifying. Heavy pre-sales or business development duties mean it isn't really an engineering role.

Also check whether I've sent you this JD before, both in this conversation and via the DB lookup in Step 0, I've accidentally resent the same posting more than once, and conversation memory alone won't catch that across sessions.

## Step 2: Tailor the CV

Work from the master version I attach. The master is deliberately over-long; tailoring means cutting and reordering, not inventing.

**Structure.** Sections are Education, Professional Experience, Projects & Research, Skills. Reorder freely, for a research-heavy or vision-heavy role, Projects can go before Professional Experience; for a role where the internships carry the case, keep experience first. Internships don't have to stay in reverse-chronological order if putting the most relevant one first tells a better story; say so when you do it. Within Projects & Research specifically: reverse-chronological (most recent first) is the default, the same order the master CV itself uses, and stays the default unless a project is clearly more relevant to the JD than its neighbors, in which case say explicitly why it's being pulled out of date order. Relevance is the exception that has to be argued for, not the default assumption, if the projects picked for a CV are roughly comparable in relevance (which is the common case), sort by date, full stop. Recheck this explicitly whenever a project set carries over from a previous tailored CV or gets edited (e.g. one project swapped for another), a set that was in order before can silently fall out of order after a partial edit, verify the dates top-to-bottom before calling the file done.

**What to cut.** Three internships (DiliTrust, DeepWise, Orange Labs) and six projects in the master. Pick what the specific JD calls for. A project that's obviously filler reads worse than a shorter CV, don't pad. Drop bullets that don't earn their space for this role, and say which ones you dropped and why. Do not collapse dropped projects into a catch-all "other projects" line, either a project earns its own entry or it's left off entirely. When two projects are comparably relevant (or when a page-2 gap needs filling and there's no single clearly-best pick), prefer the more recent one -- a 2025 project reads better than a 2021-2022 one at the same relevance level, don't reach for the oldest project in the master just because it happens to touch the JD's domain. Data Joker (10/2021 -- 05/2022) is commented out of `cv_base.tex` and retired from the default rotation for exactly this reason -- it's the oldest project in the master. Only pull it back in for a job closely tied to its actual domain (industrial/drilling time-series, synthetic data via GAN), never as a generic filler pick.

**Emphasis.** Match the JD's own vocabulary where it's honest to do so, if they say "compréhension de documents," use "document understanding" rather than "entity extraction." Rename Skills category headers to fit the role (e.g. "NLP & Document Understanding" vs "GenAI & Agentic Systems" vs "Deep Learning" / "Medical Imaging"). Reframing existing work in the JD's terms is fine; adding capabilities I don't have is not.

**Skills section is reuse-only, and always needs sign-off.** The items inside each Skills category (not just the headers) must come from the master CV's existing Skills lines, selected, merged across categories, or trimmed. Never build a new skill phrase by reading through Professional Experience or Projects bullets and extracting a technique mentioned there, even when it's true and even when it would strengthen the JD match. If a technique isn't already an item in the master's Skills section, it doesn't go in Skills, full stop; it can still show up in a Projects/Experience bullet if it's genuinely part of that story. Headers can be renamed and categories merged or dropped freely, but before writing any change to a specific CV's Skills section, propose it and wait for approval, don't edit the file first and explain after.

**Honesty.** Never add a tool or technique I haven't actually used. My target companies run live technical interviews and I will be asked to walk through anything on the page. If the JD requires something I lack (RAG implementation, FastAPI, cloud platforms, SQL, Kubernetes, Java, front-end), leave it off and tell me it's a gap so I can decide how to address it in the cover letter or interview. If you notice a claim in the master CV that's no longer accurate, flag it rather than propagating it.

## Step 3: Formatting rules

- Two pages exactly. Not 1.4 pages with a half-empty second page, not 2.1 pages.
- Compile and render using the invocations in Step 0.5 (bare `pdflatex`/`pdftoppm` won't be found on PATH), and actually look at the rendered pages before showing me the result. Report the page count.
- If a section heading lands at the bottom of a page with its content pushed to the next, add `\needspace{N\baselineskip}` before it.
- If a page ends with a large gap, the fix is adjusting content volume (add a project back, trim a bullet, tighten a Skills line), not fighting LaTeX.
- Don't change the preamble, custom commands, geometry, or fonts.

## Step 4: Writing conventions

- No em-dashes or en-dashes as sentence connectors. Use commas, semicolons, or separate sentences. This applies to CV bullets and cover letters alike.
- Percentage points in absolute terms ("raising precision by 1.6 points"), not relative.
- Lead with the result, then the method. STAR structure where it fits.
- Say "production LLM" or "production baseline" rather than naming internal systems; specific open model names (Qwen3.5-4B) are fine and preferred.
- Escape `%` as `\%` in LaTeX.
- Header tagline is always the same generic line, identical to `templates/cv_base.tex`, on every tailored CV: "Seeking a full-time Machine Learning role (CDI) from September 2026 — Île-de-France, open to mobility." No "targeting `<role>` at `<company>`" clause, ever, no matter how well a title or company name would read there. Always include "from September 2026" (`jobhunter/tailor/engine.py`'s `AVAILABILITY` constant, update both if the date changes). Once a header is in a CV, it is locked: do not regenerate or reword it on a later pass over the same file, even while editing other sections.
- Contact email: hongming.marius.fang@gmail.com
- Education entry for Beihang: "CTI-accredited French engineering degree from a joint program between Beihang University and the Groupe des Écoles Centrales, also awarding Chinese Bachelor & Master of Science degrees." No "(titre d'ingénieur)" in parentheses.

## Step 5: Cover letter, if I ask for one

English, matching the CV. Four or five paragraphs.

Open with the most specific connection between my background and this role, a matching domain, a matching technique, something in the JD only someone who read it carefully would pick up on. Don't open with "I am writing to apply for."

Build the middle around one or two concrete stories with numbers, not a list of skills. The strongest material is usually a diagnosis-and-fix arc: something broke, I found out why, I fixed it, here's the number.

Name a real gap if the JD has an obvious requirement I don't meet, and frame it honestly rather than hiding it. This has consistently felt better than pretending.

Close with availability (September 2026, Paris) and what specifically draws me to this company.

Same no-dashes rule. Don't overclaim, don't inflate internships into full-time roles.

## Working style

Be direct. If a role is a bad fit, say so in the first line rather than burying it after three paragraphs of context. If I'm about to put something inaccurate on my CV, push back even if I asked for it. If I suggest a change you think is wrong, tell me why before doing it, then do it if I still want it.

Explain what you changed and why, briefly. I'll usually have an opinion.

When I say a status changed (applied, heard back, rejected, interview scheduled), update the DB per Step 0 in the same turn, don't wait for a separate "update the DB" instruction. That's the gap that let three applications' statuses go stale earlier.
