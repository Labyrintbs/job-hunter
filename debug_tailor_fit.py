"""Step-by-step run of the CV tailoring for one job, for debugging in VS Code.

Uses the plan saved by an earlier tailoring (the LLM's answer and the summary), so it makes no
LLM call. It does run LaTeX and pdftotext for real, and writes to a temp folder, never to data/cv/.
Set a breakpoint on any numbered step and step through with F10 / F11.
"""
import json
import tempfile
from pathlib import Path

from jobhunter import db
from jobhunter.models import Job
from jobhunter.tailor import engine, fit

JOB_ID = 3828
PLAN_FILE = Path("data/cv/3828-groupagora/cv-20261005-230129-718.plan.json")


def show(title: str, plan: fit.Plan) -> None:
    print(f"\n== {title}")
    for i, e in enumerate(plan.experiences):
        print(f"experience {i}: shown={e.kept} keep={e.keep} scores={e.scores} lines={e.lines} base={e.base_lines:.1f}")
    for i, e in enumerate(plan.projects):
        print(f"project {i}: shown={e.kept} keep={e.keep} scores={e.scores} lines={e.lines} base={e.base_lines:.1f}")
    for i, s in enumerate(plan.skills):
        print(f"skills line {i}: trimmable={s.trimmable} main={s.main} keep={s.keep} scores={s.scores}")
    print(f"modules: {plan.modules}")


# 1. the job and the saved LLM answer
with db.connect() as conn:
    row = conn.execute("select source, external_id, title, company, description, location from jobs where id=?",
                       (JOB_ID,)).fetchone()
job = Job(source=row[0], external_id=row[1], title=row[2], company=row[3], description=row[4], location=row[5])
stored = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
language = stored["language"]

# 2. the draft: parsed master CV, the selection with its fit plan, the summary (all from `stored`)
draft = engine._draft(job, role_category=stored["role_category"], language=language, stored=stored)
print("blocks:", len(draft.parsed.experiences), "experiences,", len(draft.parsed.projects), "projects")
print("kept experiences:", [b.text.split("{")[1].split("}")[0] for b in draft.selection.experiences])
print("summary:", draft.summ.text)

# 3. the fit plan before any trimming: every bullet and skill item is kept
show("fit plan before fitting", draft.plan)

# 4. the fit loop: render, compile, measure, remove the lowest-scored items until two pages
out_dir = Path(tempfile.mkdtemp(prefix="tailor_debug_"))
fitted = engine._fit_cv(draft, out_dir, "cv-debug")

# 5. what the loop decided
show("fit plan after fitting", fitted.plan)
print("\nstatus:", fitted.status, "| compiles:", fitted.compiles, "| note:", fitted.note or "-")
print("\n".join(fitted.log))
print("pages:", fitted.layout.pages if fitted.layout else None)

# 6. the final compile
pdf = engine.compile_tex(fitted.tex, out_dir, name="cv-debug", expected_pages=2)
print("\nPDF:", pdf)
