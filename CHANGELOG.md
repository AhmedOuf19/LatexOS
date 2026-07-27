# Changelog

All notable changes to LaTeX Studio are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-07-27

First tagged release. Everything below is included.

### Changed
- Dependencies brought up to date: FastAPI 0.140.1, uvicorn 0.51.0,
  python-multipart 0.0.32, aiofiles 25.1.0, pytest 9.1.1 with
  pytest-asyncio 1.4.0. Verified end-to-end on the upgraded stack (this pulled
  Starlette from 0.41 to 1.3, a major version).
- `pytest` and `pytest-*` are grouped for Dependabot, because pytest-asyncio 1.x
  requires pytest >= 8.4 and cannot be upgraded on its own.

### Fixed
- **Automatic package installation now works on a fresh install.** A newly
  installed TinyTeX ships a package manager (`tlmgr`) older than the online
  repository, and TeX Live then refuses to install *anything* until it updates
  itself. The result was a confusing `File 'x.sty' not found` on the first
  document that needed any extra package — even though auto-install was on.
  `install.bat` now updates the package manager as part of setup, and if the
  situation is ever hit at compile time the app self-updates once and retries.
  On Windows `tlmgr` reports this failure with exit code 0, so it is detected
  from the message text rather than the status code.
- `_safe_filename()` now splits on both `/` and `\` on every platform. On Linux,
  `Path().name` does not treat `\` as a separator, so a Windows-style upload
  name survived sanitising intact (this was also failing CI on Ubuntu).

### Added
- Contributor setup for safe outside contributions: `CONTRIBUTING.md` (including
  the security invariants and the test that locks each one),
  `CODE_OF_CONDUCT.md`, `CODEOWNERS`, issue/PR templates and Dependabot.
- `tests/test_invariants.py` — 10 tripwires for security defaults that
  previously had no test (loopback bind, trusted hosts, the `/api/compile`
  shell-escape default, the extension deny-list, the ZIP member cap, and that no
  workflow uses `pull_request_target` or a writable token).
- CI hardening: least-privilege `permissions`, job timeouts, a concurrency
  group, a pinned TinyTeX release, and `LATEX_STUDIO_REQUIRE_LATEX=1` in the
  nightly job so the LaTeX-dependent security tests can never silently skip.

## [1.1.2] - 2026-07

### Added
- **Shell-escape checkbox** next to the Compile button. Packages like `minted`
  need `\write18`, which is disabled by default for safety — previously the only
  way to enable it was an environment variable before launch. It is now a
  per-compile opt-in (remembered between visits, with a warning when switched
  on); the safe default is unchanged. The API accepts a `shell_escape` form
  field and reports the setting back in the compile response.
- **Plain-language diagnostics** for three common failures, each with the exact
  fix: a document that needs shell-escape (`minted`), a section title hyperref
  cannot turn into a PDF bookmark (usually `\url{}` — it emits a raw `%` that
  comments out the rest of the bookmark file), and Unicode characters pdflatex
  cannot typeset (points at the xelatex/lualatex engine selector).

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
