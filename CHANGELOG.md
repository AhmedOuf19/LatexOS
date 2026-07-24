# Changelog

All notable changes to LaTeX Studio are documented here.
This project follows [Semantic Versioning](https://semver.org/).

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
