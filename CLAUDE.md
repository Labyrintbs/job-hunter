# Job Hunter — working notes

## CV tailoring

**Full workflow (evaluate-then-tailor rules, formatting rules, writing
conventions, header tagline convention): `templates/cv_tailoring_workflow.md`.
Read it before tailoring a CV — do not improvise the process.**

The header tagline is a fixed pattern — see that doc's Step 4. Once a header
has been approved for a given CV, it is locked: never regenerate or reword it
on a later editing pass over the same file.

## CV rendering & visualization tools

This machine has the tools needed to compile and visually check tailored CVs,
but none of them are on PATH by default for non-interactive shells (the Bash
tool here doesn't source `~/.zshrc`). Use the full invocations below instead
of re-discovering these each session.

### Compile LaTeX -> PDF

MacTeX 2021 is installed at `/usr/local/texlive/2021`, with `latexmk` and
`pdflatex` symlinked into `/Library/TeX/texbin` (also added to PATH in
`~/.zshrc` for interactive shells, but not for the Bash tool):

```bash
cd data/cv/<job_id>-<company>/
PATH="/Library/TeX/texbin:$PATH" latexmk -pdf -interaction=nonstopmode cv.tex
```

### Render PDF pages -> PNG (to actually look at the layout)

`pdftoppm` is not on PATH and the Homebrew `poppler` install is
incomplete/broken (interrupted install left a newer `libtiff` behind — see
"Known breakage" below). Use the working `pdftoppm` binary from the `dalas`
conda env instead, which carries its own compatible `liblcms2`:

```bash
cd data/cv/<job_id>-<company>/
DYLD_LIBRARY_PATH="/Users/tuboshu/opt/anaconda3/envs/dalas/lib" \
  /Users/tuboshu/opt/anaconda3/envs/dalas/bin/pdftoppm -png -r 100 cv.pdf page
```

This produces `page-1.png`, `page-2.png`, etc. — read them with the Read tool
to visually verify page count, section breaks, and whitespace before handing
a tailored CV back for review. Delete the PNGs afterward (they're scratch
output, not deliverables).

### Known breakage (as of 2026-09-03)

- **Homebrew `ghostscript`** (`/opt/homebrew/bin/gs`) is currently broken:
  `Library not loaded: libtiff.5.dylib`. A `brew install poppler` was
  interrupted partway through and left a newer `libtiff` (4.7.2, ships only
  `libtiff.6`) on disk without finishing poppler itself. `gs` was usable
  before that and is a fine fallback (`gs -dNOPAUSE -dBATCH -sDEVICE=png16m
  -r100 -sOutputFile=page-%d.png cv.pdf`) if someone runs `brew reinstall
  ghostscript` to relink it.
- **Homebrew `poppler`** itself never finished installing (no keg under
  `/opt/homebrew/Cellar/poppler`) — don't assume `pdftoppm` is on PATH.
- A `poppler` package also exists under `~/miniforge3/pkgs/poppler-*` but its
  `pdftoppm` is missing `liblcms2.2.dylib` and will not run as-is.
