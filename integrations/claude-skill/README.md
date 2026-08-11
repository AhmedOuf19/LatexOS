# Claude Code skill

Lets [Claude Code](https://claude.com/claude-code) compile LaTeX through this
project. Once installed, asking *"render this LaTeX to a PDF"* — in any folder,
without naming the tool — makes Claude call the CLI, read the structured errors,
and report what is wrong and where.

## Install

1. Install LaTeX Studio somewhere permanent, e.g. `C:\Claude Skills\latex`:

   ```bat
   git clone https://github.com/AhmedOuf19/LatexOS.git "C:\Claude Skills\latex"
   cd /d "C:\Claude Skills\latex"
   install.bat
   ```

2. Copy the skill into your personal skills directory:

   ```bat
   mkdir "%USERPROFILE%\.claude\skills\latex-to-pdf"
   copy SKILL.md "%USERPROFILE%\.claude\skills\latex-to-pdf\SKILL.md"
   ```

3. **Edit the paths in `SKILL.md`** if you installed anywhere other than
   `C:\Claude Skills\latex` — the file names that location several times.

4. Start a new Claude Code session. Skills load at startup.

## Verify

Ask Claude, without mentioning this project:

> Render this LaTeX to a PDF:
> ```latex
> \documentclass{article}
> \begin{document}Hello.\end{document}
> ```

It should produce a PDF without asking you how, and without trying to install
anything.

## What it does beyond "run the compiler"

- Reads the JSON result and reports errors **with file and line**, rather than
  pasting the log.
- Distinguishes the three outcomes that matter: build failed, **build
  "succeeded" but the PDF is silently wrong**, and cosmetic noise.
- Groups repetitive output — 119 overfull boxes are reported as one layout
  problem, not 119 lines.
- Suggests how to avoid the problem next time, not just how to patch this one.
- Verifies a proposed fix on a copy before claiming it works.

## Defaults, and the security choice in them

The skill's default command passes `--engine xelatex --shell-escape`, because
the author's documents need both.

**`--shell-escape` lets a document run programs on your machine.** That is a
reasonable default for compiling *your own* work and a bad one for anything you
downloaded. The skill instructs Claude to drop the flag for untrusted documents
and to ask before re-enabling it.

The CLI itself still defaults shell-escape to **off** — this is a skill-level
choice, so anything else calling `backend.cli` stays safe. If you would rather
opt in per-compile, delete `--shell-escape` from the command in `SKILL.md`.
