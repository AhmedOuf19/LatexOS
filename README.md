# LaTeX Studio 🎓

A powerful web application for compiling LaTeX projects to PDF – inspired by Overleaf.
Upload your entire LaTeX project (`.tex`, `.bib`, images, custom classes) and get a beautiful PDF in seconds.

## Features

- 📁 **Multi-file upload** – Drag & drop individual files or a `.zip` of your whole project
- ✏️ **In-browser editor** – Full code editor with syntax highlighting (Monaco Editor)
- 🖼️ **Image preview** – View images directly in the editor pane
- 🔍 **Smart main file detection** – Automatically finds your `main.tex`
- ⚙️ **Multiple engines** – Choose between `pdflatex`, `xelatex`, or `lualatex`
- 📚 **Bibliography support** – Automatically handles `bibtex` and `biber`
- 🎨 **Custom classes & styles** – Upload your `.cls` and `.sty` files
- 📊 **Structured log viewer** – Color-coded errors, warnings, and bad-box alerts
- 📥 **PDF download** – Download or preview inline in your browser
- 🔒 **Security** – Shell escape disabled, path traversal blocked, file type whitelist
- 🚀 **One-click setup** – Automatically installs Python & MiKTeX if needed

---

## Quick Start (Beginner-Friendly)

### Requirements

- **Windows 10 or 11**
- **Internet connection** (only needed the first time to download dependencies)

### How to Run

1. **Download** (or clone) this project folder
2. **Double-click** `Launch LaTeX Studio.bat`
3. **Wait** — the script will automatically:
   - ✅ Find or install Python
   - ✅ Create a virtual environment
   - ✅ Install required Python packages
   - ✅ Find or install MiKTeX (LaTeX compiler)
   - ✅ Open your browser to the app
4. **Done!** Upload your `.tex` files and click **Compile PDF**

> **First-time setup** may take 3-5 minutes (downloading Python ~25 MB + MiKTeX ~250 MB).  
> After that, launching takes only a few seconds.

> **No admin rights required.** Everything installs to your user account.

---

## How to Use

1. **Drag & drop** your LaTeX files onto the upload zone (or click to browse)
2. **Upload a single `.zip`** of your entire project for convenience
3. The app **auto-detects** your main `.tex` file (or select it from the dropdown)
4. **Edit files** directly in the browser using the built-in code editor
5. Choose your **LaTeX engine** (pdflatex, xelatex, lualatex)
6. Click **Compile PDF**
7. View the **PDF inline** or **download** it
8. Check the **log panel** for errors and warnings

---

## Supported File Types

| Type | Extensions |
|------|-----------|
| LaTeX source | `.tex`, `.cls`, `.sty`, `.bst`, `.dtx`, `.ins` |
| Bibliography | `.bib` |
| Images | `.png`, `.jpg`, `.jpeg`, `.pdf`, `.eps`, `.svg`, `.tif`, `.bmp` |
| Fonts | `.ttf`, `.otf` |
| Data | `.csv`, `.txt`, `.dat` |
| Archive | `.zip` (auto-extracted) |

---

## Running Tests

```bash
# Activate virtual environment first
.venv\Scripts\activate

# Run all tests
pytest tests/ -v

# Run without LaTeX-dependent tests (no MiKTeX required)
pytest tests/ -v -m "not requires_latex"
```

---

## Project Structure

```
MD_to_Latex/
├── Launch LaTeX Studio.bat  ← Double-click to run (auto-setup)
├── run.py                   ← Server entry point
├── requirements.txt
├── README.md
├── .gitignore
├── backend/
│   ├── main.py              ← FastAPI app + routes
│   ├── compiler.py          ← LaTeX compilation engine
│   ├── file_manager.py      ← Upload/session management
│   ├── log_parser.py        ← Error/warning extraction
│   └── config.py            ← Configuration
├── frontend/
│   ├── index.html           ← Main UI
│   ├── style.css            ← Dark mode styling
│   └── app.js               ← Frontend logic (Monaco editor, file tree)
├── .venv/                   ← Virtual environment (auto-created)
├── uploads/                 ← Session workspaces (auto-created)
└── tests/
    ├── test_compiler.py     ← Compilation scenario tests
    ├── test_api.py          ← API endpoint tests
    └── fixtures/            ← Sample LaTeX projects
```

---

## Advanced Configuration

Override defaults via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LATEX_BIN_PATH` | auto-detect | Path to LaTeX binaries directory |
| `LATEX_ENGINE` | `pdflatex` | Default engine |
| `LATEX_TIMEOUT` | `120` | Max compile time (seconds) |
| `MAX_UPLOAD_MB` | `100` | Max total upload size |
| `SESSION_TTL` | `3600` | Session lifetime (seconds) |

Example:
```bash
set LATEX_BIN_PATH=C:\Program Files\MiKTeX\miktex\bin\x64
set LATEX_TIMEOUT=180
python run.py
```

---

## Manual Setup (Advanced Users)

If you prefer to set things up manually instead of using the auto-launcher:

### Prerequisites

1. **Python 3.10+** — [python.org](https://www.python.org/downloads/)
2. **MiKTeX** — [miktex.org/download](https://miktex.org/download) (or TeX Live from [tug.org/texlive](https://tug.org/texlive/))

### Steps

```bash
# 1. Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Run pre-flight checks
python run.py --check

# 4. Start the application
python run.py --port 8000 --open-browser
```

---

## Troubleshooting

### The .bat window opens and closes immediately
- Right-click the `.bat` file → **Run as administrator** (only if per-user install fails)
- Or open a Command Prompt, `cd` to the project folder, and run: `"Launch LaTeX Studio.bat"`

### "pdflatex not found"
- The launcher auto-installs MiKTeX, but if it failed:
  - Install MiKTeX manually from [miktex.org/download](https://miktex.org/download)
  - Or set `LATEX_BIN_PATH` environment variable
- Run `python run.py --check` to diagnose

### "Compilation failed" with missing packages
- MiKTeX is configured for auto-install; first compilations may take longer
- Open **MiKTeX Console** and enable automatic package installation if needed

### Upload fails with large files
- Default limit is 100 MB total. Override with `set MAX_UPLOAD_MB=200`

---

## API Reference

Full interactive API docs are available at `http://localhost:8000/docs` when the app is running.

| Endpoint | Method | Description |
|---------|--------|-------------|
| `/api/status` | GET | Check LaTeX tool availability |
| `/api/upload` | POST | Upload project files |
| `/api/compile` | POST | Compile the project |
| `/api/pdf/{session}` | GET | Download/view PDF |
| `/api/log/{session}` | GET | Get compilation log |
| `/api/files/{session}/{path}` | GET | Read a file |
| `/api/files/{session}/{path}` | PUT | Write/save a file |
| `/api/cleanup/{session}` | DELETE | Remove session |
