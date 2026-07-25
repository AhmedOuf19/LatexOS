# Changelog

All notable changes to LaTeX Studio are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-07

### Changed
- Raised the default limits so large projects work out of the box (all still
  overridable via environment variables): compile timeout 120s → **600s** (plus
  package-install time on top), max upload 100 MB → **500 MB**, ZIP extraction
  cap → **2 GB**, ZIP member count → **10000**, log read → **32 MB**, and the
  session inactivity timeout 1h → **6h**.

Fixes found by an adversarial review of the on-demand installer and by running
real documents on the portable TinyTeX build.

### Fixed
- **On-demand package install now works on TinyTeX.** TinyTeX ships `tlmgr` as
  `tlmgr.bat`; the binary resolver only looked for `.exe`, so missing packages
  (e.g. `listingsutf8.sty`) were never installed and the compile failed. The
  resolver now finds `.bat`/`.cmd` scripts.
- **Broken documents are no longer reported as clean.** The log parser dropped
  file:line errors whose text lacked a hard-coded keyword ("Too many }'s",
  "Double superscript", "Illegal unit of measure", "Misplaced alignment tab",
  …). It now treats every `file:line:` line as an error unless it is clearly a
  warning, and the UI shows "compiled with N error(s)" when a PDF is produced
  despite errors.
- Errors reported from a Windows absolute (drive-lettered) path — i.e. from
  inside the folder-local TinyTeX — are parsed correctly.
- The installer now: installs every missing package (not a fixed cap of 9),
  verifies each install actually resolved with `kpsewhich` (because `tlmgr.bat`
  always exits 0), never guesses a package name from the filename stem
  (`tikz.sty` → `pgf`, not `tikz`), skips user assets and forgotten `\input`
  files, gives package downloads their own time budget so they don't starve the
  compile, and reports a clear message when a package can't be fetched.
- Missing bibliography styles (`.bst`) and driver/config/font-definition files
  (`.def`/`.cfg`/`.fd`/`.ldf`/`.enc`) now trigger installation; the bibtex
  `.blg` is scanned too.
- The manual fallback path now runs `makeindex`/`makeglossaries` for indices and
  glossaries, gates bibtex on `\bibdata` (not `\citation`), and respects the
  timeout at every step.
- A locked, undeletable stale PDF can no longer report a failed compile as
  success (freshness is verified by modification time).
- `/api/pdf` and `/api/log` fall back to disk after a server restart; the
  in-memory maps are pruned by the background cleaner.
- `PUT /api/files` now enforces the size limit while streaming, like uploads.
- No false "bibliography not found" warning on successful builds.

## [1.1.0] - 2026-07

A large correctness, security and portability overhaul.

### Security
- **Shell-escape is now disabled by default.** Arbitrary command execution via
  `\write18` is blocked unless you explicitly opt in with
  `LATEX_ALLOW_SHELL_ESCAPE=1`. The docstring/README claims now match reality.
- Confined `openin_any`/`openout_any` so a document cannot read or write files
  outside its own workspace (blocks `\input{C:/…/secret}` exfiltration).
- Removed wildcard CORS; the API now refuses cross-origin browser requests and
  requires a per-instance token, closing the "any website drives your local
  API" drive-by vector. Added trusted-host protection.
- Fixed an arbitrary-file-read hole in the static file route (`%2e%2e`
  traversal) by confining every served path to the frontend directory.
- Hardened ZIP handling: no more extensionless files (blocked a `.latexmkrc`
  RCE), zip-bomb caps (extracted size + member count), and sanitized member
  names (blocked stored XSS from filenames).
- Streamed uploads with an early size cap; added a size cap to file saves.

### Fixed
- The frontend no longer crashes on load (a missing `openNewTabBtn` element
  used to abort initialization and disable the whole UI).
- A failed compile can no longer report success from a leftover PDF — build
  artifacts are cleared before every compile.
- The log parser now captures `file:line:` errors produced by
  `-file-line-error` (previously the most common errors were dropped).
- `/api/pdf` serves the exact PDF the compiler produced instead of guessing by
  modification time (an uploaded figure could win before).
- biber now runs for biblatex projects in the manual fallback path.
- `TEXINPUTS`/`BIBINPUTS`/`BSTINPUTS` include the workspace recursively, so
  resources in sub-folders are found.
- A single global timeout now bounds the whole compile (was up to ~2.25×).
- Editor saves report real failures; opening an image after text no longer
  corrupts files; the compile button no longer wedges on an early error.
- Session cleanup on tab close actually works (fetch keepalive, not sendBeacon).

### Added
- **Portable, self-contained install:** Python, LaTeX (TinyTeX), the Monaco
  editor and fonts all install *into the project folder*.
- **Separated launcher scripts:** `install.bat`, `Launch LaTeX Studio.bat`
  (run), and `update.bat`, each a thin wrapper over a PowerShell script.
- Self-hosted fonts and vendored Monaco — the app works fully offline.
- Session persistence: a browser refresh restores your project.
- On-demand LaTeX package installation via `tlmgr` for TinyTeX/TeX Live.
- Keyboard-accessible file tree; a plain-textarea editor fallback.
- Test infrastructure: registered `requires_latex` marker, isolated temp
  workspaces, security regression tests, and a CI workflow.

## [1.0.0]
- Initial release (single monolithic launcher, MiKTeX auto-install).
