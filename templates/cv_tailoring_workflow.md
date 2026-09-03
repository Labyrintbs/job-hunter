# CV Tailoring Prompt

Copy everything below into a new conversation, then attach the master CV `.tex` and paste the job description.

---

## Context

I'm Hongming Fang, finishing an MSc in Computer Science (Image specialization) at Sorbonne University in September 2026. I have a CTI-accredited French engineering degree from École Centrale de Pékin / Beihang University. I'm looking for a full-time CDI in ML/NLP/LLM engineering in Île-de-France, starting September 2026.

Visa constraint: I need a CDI with gross annual base salary above the Passeport Talent "salarié qualifié" threshold (€39,582 for 2026). This is a floor, not a target — market rate for junior ML roles in Paris is €45,000–55,000.

I'll give you my master CV (LaTeX) and a job description. Two things I need from you: an honest assessment of whether the role is worth applying to, and if it is, a tailored version of the CV.

## Step 1: Evaluate the job before tailoring anything

Don't start editing until we've agreed the role is worth it. Give me a direct verdict, invest or pass, with reasoning on:

**Company type.** I'm targeting product companies (SaaS B2B, LegalTech, FinTech, HealthTech), 10–500 people. I want to avoid ESNs, staffing firms, and pure consultancies where I'd be placed on client "missions." Tells: the words *consultant*, *mission*, *chez nos clients*, *accompagner nos clients*, "cabinet de conseil," headcount in the thousands with offices in 20+ countries. Note that some consultancies have genuinely strong technical roles, flag those as a judgment call rather than an automatic pass, since I've decided case by case.

**Seniority.** Titles with Staff / Principal / Lead / Head of are out of range. "Senior" depends on the stated years. Hard year requirements ("at least 2 years," "3–7 years") are real filters, my ~15 months of internships won't clear a 3-year bar, and you should say so plainly rather than helping me rationalize it. Roles that explicitly count internships or say "jeune diplômé" are the sweet spot.

**Technical direction.** My work is LLM/NLP model development: fine-tuning, agentic orchestration, evaluation design. Roles that are actually MLOps/infrastructure (Kubernetes, Airflow, Terraform, CI/CD, Spark/Kafka, feature stores, data warehouses) or Data Engineering (ETL, dbt, BigQuery, Snowflake) are a different career track, say so directly. Quick heuristic: if more than half the listed skills are infrastructure tooling, it's not my direction. Full-stack roles requiring TypeScript/React/Node are also out.

**Other flags.** Defense or dual-use work is effectively closed to me as a Chinese national (security clearance). Salary below the visa threshold is disqualifying. Heavy pre-sales or business development duties mean it isn't really an engineering role.

Also check whether I've sent you this JD before, I've accidentally resent the same posting more than once.

## Step 2: Tailor the CV

Work from the master version I attach. The master is deliberately over-long; tailoring means cutting and reordering, not inventing.

**Structure.** Sections are Education, Professional Experience, Projects & Research, Skills. Reorder freely, for a research-heavy or vision-heavy role, Projects can go before Professional Experience; for a role where the internships carry the case, keep experience first. Internships don't have to stay in reverse-chronological order if putting the most relevant one first tells a better story; say so when you do it. Within Projects & Research specifically: order by relevance to the JD when that's clearly justified (say why); when there isn't a real relevance signal to sort on, default to reverse-chronological (most recent first), the same default the master CV itself uses, rather than leaving them in an arbitrary order.

**What to cut.** Three internships (DiliTrust, DeepWise, Orange Labs) and six projects in the master. Pick what the specific JD calls for. A project that's obviously filler reads worse than a shorter CV, don't pad. Drop bullets that don't earn their space for this role, and say which ones you dropped and why. Do not collapse dropped projects into a catch-all "other projects" line, either a project earns its own entry or it's left off entirely.

**Emphasis.** Match the JD's own vocabulary where it's honest to do so, if they say "compréhension de documents," use "document understanding" rather than "entity extraction." Rename Skills category headers to fit the role (e.g. "NLP & Document Understanding" vs "GenAI & Agentic Systems" vs "Deep Learning" / "Medical Imaging"). Reframing existing work in the JD's terms is fine; adding capabilities I don't have is not.

**Honesty.** Never add a tool or technique I haven't actually used. My target companies run live technical interviews and I will be asked to walk through anything on the page. If the JD requires something I lack (RAG implementation, FastAPI, cloud platforms, SQL, Kubernetes, Java, front-end), leave it off and tell me it's a gap so I can decide how to address it in the cover letter or interview. If you notice a claim in the master CV that's no longer accurate, flag it rather than propagating it.

## Step 3: Formatting rules

- Two pages exactly. Not 1.4 pages with a half-empty second page, not 2.1 pages.
- Compile with `pdflatex`, convert with `pdftoppm`, and actually look at the rendered pages before showing me the result. Report the page count.
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
