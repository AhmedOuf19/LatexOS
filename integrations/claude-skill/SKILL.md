---
name: latex-to-pdf
description: Compile LaTeX to PDF locally. Use whenever the user wants LaTeX rendered, compiled, or turned into a PDF — including "render this LaTeX", "compile my .tex", "make a PDF of this", "build my thesis/paper/CV", or when they paste LaTeX source and ask to see it as a document. Also use to diagnose LaTeX compile errors, or to typeset a document you were asked to write. Runs fully offline via a bundled TeX distribution; no internet or Overleaf account needed.
---

# LaTeX → PDF

Compiles LaTeX to PDF on this machine using **LaTeX Studio**, installed at
`C:\Claude Skills\latex`. It bundles its own Python and TeX distribution, so it
works offline and does not depend on anything else being installed.

## The one command

```bat
"C:\Claude Skills\latex\latex-pdf.bat" compile "<path-to.tex>" --engine xelatex --shell-escape --json
```

**Use these defaults unless there is a reason not to.** This user's documents
routinely need both, and compiling without them produces a PDF that *looks*
fine while silently dropping Greek letters and leaving code unhighlighted:

- `--engine xelatex` — handles Unicode (θ, λ, ₀, accents, CJK) natively.
  pdflatex cannot, and **still emits a PDF with those characters missing**.
- `--shell-escape` — required by `minted`. Without it, code blocks lose their
  syntax highlighting. See the safety note below for when to drop it.

Works from any working directory, and relative paths resolve against **your**
cwd, not the project's. Quote the launcher path — it contains a space.

`--json` is strongly preferred: you get the PDF path plus each error with its
file and line, instead of having to parse a human report.

### Common variations

| Need | Change |
|---|---|
| Put the PDF somewhere specific | add `-o "C:\path\out.pdf"` |
| Treat "PDF produced but log has errors" as failure | add `--strict` |
| A whole project folder (auto-finds the main file) | pass the directory instead of a file |
| Untrusted document (see safety note) | drop `--shell-escape` |
| Faster build, plain ASCII document | `--engine pdflatex` (the tool's own default) |
| Check the toolchain is working | `"C:\Claude Skills\latex\latex-pdf.bat" check` |

## How to use it

**1. Get the LaTeX into a file.** If the user pasted source rather than pointing
at a file, write it to a `.tex` file first — in their working directory if they
want to keep it, otherwise a temp directory. Never invent a document they did
not ask for.

**2. Compile.** Run the command above.

**3. Report the outcome, not the mechanics.** Tell the user where the PDF is.

**4. If there is anything in `errors`, `warnings` or `badboxes`, analyse it.**
Don't just relay LaTeX's wording — see *Analysing problems* below. This applies
even when the compile "succeeded": a PDF can be produced and still be wrong.

## Reading the result

Exit codes are the contract:

| Code | Meaning | What to do |
|---|---|---|
| `0` | PDF produced | Report the path. **Still check `errors`** — LaTeX often emits a PDF despite recoverable errors. |
| `1` | Compile failed, no PDF | Read `errors[]`, fix the cause, retry. |
| `2` | Bad path / not a `.tex` | Check the file path. |
| `3` | No LaTeX distribution | Tell the user to run `C:\Claude Skills\latex\install.bat`. |

The JSON payload:

```json
{
  "success": true,
  "pdf": "C:\\...\\paper.pdf",
  "engine": "pdflatex",
  "summary": "Compilation completed with no issues.",
  "errors":   [{"message": "Undefined control sequence.", "file": "paper.tex", "line": 12}],
  "warnings": [...],
  "badboxes": [...],
  "log_path": "C:\\...\\paper.log"
}
```

Every entry carries `file` and `line`, so you can open the source at that exact
spot rather than guessing.

---

## Analysing problems

**Never just echo LaTeX's message.** Its wording is accurate but written for
TeX experts — it names the symptom, almost never the cause. Your job is to go
from the message to *the line in the user's source that is responsible*, and to
tell them how to stop it happening again.

### Step 1 — Triage by consequence, not by severity label

LaTeX's own labels are misleading. Sort what you got into these three, and lead
with the one that actually matters:

| Class | How to spot it | How to treat it |
|---|---|---|
| 🔴 **Breaks the build** | `success: false`, no PDF | Fix first. Nothing else matters. |
| 🟠 **Silently corrupts the PDF** | `success: true` **but** errors present | **The dangerous one.** The user has a PDF that looks fine and is wrong — missing characters, unhighlighted code, dead links. Always call this out explicitly. |
| ⚪ **Cosmetic** | `badboxes`, most `warnings` | Summarise as a count. Do not list them one by one. |

### Step 2 — Find the real source

The reported location is where LaTeX *noticed* the problem, which is often not
where it was *caused*. Open the file and read the actual line before explaining
anything.

- An error at `\begin{document}` almost never lives there — it comes from the
  preamble, or from a `.aux`/`.out` file written by a previous pass.
- Errors inside a `.sty` under `tinytex/` are **not** package bugs; something in
  the user's document drove the package there.
- Read `file`/`line` from the JSON, then quote the offending line back to the
  user so they can see it.

### Step 3 — Group before reporting

Real documents produce repetitive output. 119 overfull boxes are one problem,
not 119. Collapse them:

> "119 overfull boxes, all in the two-column layout — 94 of them in Chapter 5's
> long equations."

Report **distinct causes**, with a count each. Never paste a wall of near-identical lines.

### Step 4 — For each distinct problem, give four things

1. **What it is** — in plain language, not TeX jargon.
2. **Where** — `file:line`, with the offending line quoted.
3. **Why it happened** — the actual mechanism.
4. **How to avoid it next time** — the general rule, not just this one fix.

Point 4 is what the user actually asked for. "Change line 30" is a patch;
"`\url{}` never works in a section title, use `\texorpdfstring{}{}`" is a lesson.

### Step 5 — Verify before you claim

If you propose a fix, **prove it**: copy the project to a temp folder, apply the
fix there, recompile, and report the error count before and after. Do not edit
the user's files to test a theory. A hypothesis that sounds right is often
wrong — say so plainly if your first guess fails.

### Common causes worth recognising

| Symptom | Real cause | Prevention rule |
|---|---|---|
| `File ended while scanning use of \@@BOOKMARK` | `\url{}`, `%` or a fragile command inside a **section title** — it writes a raw `%` into the `.out`, commenting out the closing brace | Titles should be plain text. Wrap anything else in `\texorpdfstring{typeset}{plain}` |
| `Unicode character X not set up` | pdflatex can't typeset it. **A PDF is still produced with the character missing** | Use `--engine xelatex`, or write `$\theta$` rather than a literal `θ` |
| `Package minted Error: ... unavailable or disabled` | `--shell-escape` was not passed; the code appears **unhighlighted but present** | It is in the default command; if you dropped it for an untrusted document, ask before adding it back |
| `Reference/Citation ... undefined` | label never defined, or a typo | Check the `\label` exists and matches exactly |
| `No file chapterN.tex` (warning only) | `\include` of a file that doesn't exist — **silently skipped** | Every `\include` needs a real file, or remove the line |
| `Overfull \hbox` | a word/equation/table wider than the column | Usually ignorable. If it's visible: `\sloppy`, a manual break, or a smaller table |
| `Token not allowed in a PDF string` | hyperref met markup it can't put in a bookmark | Same fix as `\@@BOOKMARK`: `\texorpdfstring` |

### The shape of a good report

> **The PDF compiled, but two things in it are wrong.**
>
> **1. Greek letters are missing** (silently) — `chapter3.tex:114` uses a literal
> `θ`, and pdflatex cannot typeset it, so it was dropped from the output.
> → Recompiled with xelatex; they now appear.
> → *To avoid:* use `$\theta$` in source, or always compile this document with xelatex.
>
> **2. PDF bookmarks are broken** — `chapter4.tex:30`:
> `\section{Specification of \url{COMPUTE_1D_REGRESSION}}`
> `\url` inside a title emits a raw `%`, which comments out the rest of the
> bookmark entry.
> → Fix: `\texorpdfstring{\url{...}}{COMPUTE\_1D\_REGRESSION}` (verified: 2 errors → 0)
> → *To avoid:* keep section titles plain text.
>
> Also 119 overfull boxes (cosmetic, from the two-column layout) and 5 chapters
> referenced by `\include` that don't exist.

## Fixing failures

Missing packages install themselves automatically, so a missing-package error
usually means the *name* is wrong, not that it needs installing. Otherwise:

| Error | Fix |
|---|---|
| `Unicode character ... not set up for use with LaTeX` | You are on pdflatex — recompile with `--engine xelatex` (the default above). |
| `File 'x.sty' not found` (persists after a retry) | The package name is likely misspelled, or it needs `--shell-escape`. |
| `Undefined control sequence` | A typo, or a missing `\usepackage`. The line number is in `errors[]`. |
| `Missing $ inserted` | Maths used outside maths mode. |
| `minted`/`pygmentize` errors | Add `--shell-escape`. |

Retry at most **twice** for the same document. If it still fails, show the user
the error and ask — don't keep guessing.

## Shell-escape — safety note

`--shell-escape` lets the document **run programs on this machine**. It is in the
default command above because this user's own documents need it (`minted`), and
that is a deliberate, informed choice for *their* work.

It is not a blanket permission. **Drop `--shell-escape`, and say why, when the
LaTeX did not come from the user** — a downloaded template, a paper someone
sent, a repo you cloned, or source pasted from a website. Compiling an
untrusted document with shell-escape is equivalent to running a stranger's
script.

If such a document genuinely needs it (a `minted` error appears), say so and
**ask** before re-running with it enabled.

Note the tool itself still defaults to OFF — the flag above is this skill's
choice, so anything invoking the CLI directly stays safe.

## Notes

- The PDF is written next to the `.tex` file unless `-o` says otherwise, and
  `.aux`/`.log` files appear alongside it. That is normal LaTeX behaviour.
- For multi-file projects (`\input`, `\includegraphics`, `.bib`), pass the
  **directory**. Bibliographies and cross-references are resolved automatically
  — no need to run `bibtex`/`biber` or compile repeatedly.
- To open the PDF for the user: `start "" "C:\path\to.pdf"`.
- There is also a browser UI (`C:\Claude Skills\latex\Launch LaTeX Studio.bat`)
  with a live editor and preview — mention it if the user wants to iterate on a
  document themselves rather than through you.
