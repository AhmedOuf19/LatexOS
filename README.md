# LaTeX Studio 🎓

A self-contained, offline-friendly web app that compiles LaTeX projects to PDF —
a lightweight Overleaf you run on your own machine. Upload your `.tex`, `.bib`,
images and custom classes, edit in the browser, and get a PDF in seconds.

> **Local tool, not a public server.** LaTeX Studio has no login and is meant to
> run on your own computer (`127.0.0.1`). Do not expose it to a network. See
> [SECURITY.md](SECURITY.md).

## Features

- 📁 **Multi-file upload** – drag & drop files or a `.zip` of a whole project
- ✏️ **In-browser editor** – Monaco with syntax highlighting (plain-textarea fallback)
- 🖼️ **Image preview** in the editor pane
- 🔍 **Smart main-file detection** – finds your `main.tex` automatically
- ⚙️ **Three engines** – `pdflatex`, `xelatex`, `lualatex`
- 📚 **Bibliography** – handles both `bibtex` and `biber` (biblatex)
- 🎨 **Custom classes & styles** – upload your `.cls` / `.sty`
- 📊 **Structured log viewer** – color-coded errors, warnings and bad boxes
- 📥 **Inline preview + download**
- ✨ **`minted` support** – tick the **Shell-escape** box next to *Compile* when a
  document needs it (off by default; only enable it for documents you trust)
- 🔒 **Safe by default** – shell-escape off, file access confined, traversal & zip
  attacks blocked, same-origin API
- 📦 **Fully portable** – Python, LaTeX, the editor and fonts all install *into
  the folder*; delete the folder to uninstall. Works offline after setup.

---

## Quick start (Windows)

1. **Download** or clone this folder.
2. **Double-click `install.bat`** — one-time setup. It downloads and installs,
   *into this folder*, everything the app needs (no admin rights, no changes to
   your system):
   - a portable Python (via [uv](https://github.com/astral-sh/uv))
   - a portable LaTeX distribution ([TinyTeX](https://yihui.org/tinytex/))
   - the Monaco code editor and the UI fonts
3. **Double-click `Launch LaTeX Studio.bat`** — starts the app and opens your
   browser. It picks a free port automatically and never touches other programs.
4. To upgrade everything later, double-click **`update.bat`**.

> First-time setup downloads a few hundred MB and takes a few minutes.
> After that, launching is instant and works with no internet.

If you already have a system MiKTeX or TeX Live, the app will use it and the
TinyTeX download is skipped.

---

## The three scripts

| Script | What it does |
|--------|--------------|
| `install.bat` | One-time setup. Installs everything into the folder. Safe to re-run. |
| `Launch LaTeX Studio.bat` | Starts the app (no network). Picks a free port, opens the browser. |
| `update.bat` | Updates Python packages, TinyTeX packages and the editor to the latest. |

Each `.bat` is a thin wrapper over a well-commented PowerShell script in
[`scripts/`](scripts/).

---

## How to use

1. Drag & drop your LaTeX files (or a `.zip`) onto the upload zone.
2. The app auto-detects your main `.tex` (or pick one from the dropdown).
3. Edit files in the browser; press **Ctrl+S** to save.
4. Choose an engine and click **Compile PDF**.
5. Preview inline or download; check the log panel for errors and warnings.

Your session is remembered across a browser refresh.

---

## Supported file types

| Type | Extensions |
|------|-----------|
| LaTeX source | `.tex`, `.cls`, `.sty`, `.bst`, `.ist`, `.dtx`, `.ins` |
| Bibliography | `.bib` |
| Images | `.png`, `.jpg`, `.jpeg`, `.pdf`, `.eps`, `.svg`, `.tif`, `.bmp`, `.gif` |
| Fonts | `.ttf`, `.otf`, `.pfb`, `.pfm` |
| Data | `.csv`, `.txt`, `.dat`, `.md` |
| Archive | `.zip` (extracted safely) |

---

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `LATEX_BIN_PATH` | auto-detect | Directory containing the LaTeX binaries |
| `LATEX_ENGINE` | `pdflatex` | Default engine (invalid values fall back to pdflatex) |
| `LATEX_TIMEOUT` | `600` | Max total compile time, seconds (plus any package-install time on top) |
| `LATEX_ALLOW_SHELL_ESCAPE` | `0` | Set `1` to allow `\write18` (minted) for **every** compile. Normally you just tick the **Shell-escape** box in the UI instead. **Trusted docs only.** |
| `LATEX_AUTO_INSTALL` | `1` | Auto-install missing packages (TinyTeX/TeX Live via `tlmgr`) |
| `MAX_UPLOAD_MB` | `500` | Max total upload size |
| `MAX_EXTRACTED_MB` | `2000` | Max total size extracted from a ZIP (defaults to 4× `MAX_UPLOAD_MB`) |
| `MAX_ZIP_MEMBERS` | `10000` | Max number of files inside an uploaded ZIP |
| `MAX_LOG_READ_MB` | `32` | Max size of a compile log read into memory |
| `SESSION_TTL` | `21600` | Session inactivity timeout, seconds (6 hours) |
| `LATEX_HOST` / `LATEX_PORT` | `127.0.0.1` / `8000` | Bind address |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Project structure

```
MD_to_Latex/
├── install.bat                 ← one-time setup (run this first)
├── Launch LaTeX Studio.bat     ← start the app
├── update.bat                  ← update dependencies
├── run.py                      ← server entry point + pre-flight checks
├── requirements.txt
├── pyproject.toml              ← pytest / coverage config
├── README.md · LICENSE · CHANGELOG.md · SECURITY.md
├── scripts/                    ← PowerShell install/run/update logic
│   ├── common.ps1  install.ps1  run.ps1  update.ps1
├── backend/
│   ├── main.py                 ← FastAPI app, routes, security middleware
│   ├── compiler.py             ← compilation engine (latexmk + manual passes)
│   ├── file_manager.py         ← uploads, sessions, safe ZIP extraction
│   ├── log_parser.py           ← structured error/warning extraction
│   └── config.py               ← configuration + LaTeX auto-detection
├── frontend/
│   ├── index.html · style.css · app.js
│   └── vendor/fonts/           ← self-hosted fonts (committed)
│       └── (monaco/  ← downloaded by install, gitignored)
└── tests/                      ← pytest suite + fixtures
```

Generated at install time and **not** committed: `.venv/`, `python/`,
`tinytex/`, `frontend/vendor/monaco/`, `uploads/`, `logs/`.

---

## Running the tests

```bash
# Fast: LaTeX-dependent tests auto-skip if no distribution is installed
pytest -v

# With coverage
pytest --cov=backend
```

There is no need to pass `-m "not requires_latex"` — those tests skip themselves
when LaTeX is absent.

---

## Manual setup (advanced)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py --check          # diagnose the LaTeX install
python run.py --open-browser   # start on http://127.0.0.1:8000
```

---

## API reference

Interactive docs live at `http://127.0.0.1:<port>/docs` while the app runs.

| Endpoint | Method | Description |
|---------|--------|-------------|
| `/api/status` | GET | LaTeX tool availability + settings |
| `/api/upload` | POST | Upload project files |
| `/api/compile` | POST | Compile the project |
| `/api/pdf/{session}` | GET | View (or `?download=1` to save) the PDF |
| `/api/log/{session}` | GET | Compilation log (`?parsed=true` for structured) |
| `/api/files/{session}` | GET | List files |
| `/api/files/{session}/{path}` | GET / PUT | Read / write a file |
| `/api/cleanup/{session}` | DELETE | Remove a session |

API calls require the per-instance token the page carries, and are refused from
other web origins.

---

## Contributing

Contributions are welcome — bug reports, fixes and features alike.

Start with **[CONTRIBUTING.md](CONTRIBUTING.md)**. It covers the dev setup, how
to run the tests, and the **security invariants** the project relies on (things
like shell-escape staying off by default and the path-traversal guards). Each
invariant is locked by a test, so you will know immediately if a change touches
one.

Please also read the [Code of Conduct](CODE_OF_CONDUCT.md). Found a security
problem? Report it privately — see [SECURITY.md](SECURITY.md), not a public issue.

---

## License

[MIT](LICENSE). Bundled/downloaded components (Monaco, the fonts, the LaTeX
distribution) keep their own licenses — see the LICENSE file.
