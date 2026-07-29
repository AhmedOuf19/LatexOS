# Code style & commenting standard

This project is read by beginners, by people evaluating whether to trust it with
their documents, and increasingly by AI assistants. All three need the same
thing: **code that explains its own reasoning.**

This document is the one standard. If you are adding code, match it.

---

## The one rule

> **Comments explain WHY. Code explains WHAT.**

If a comment restates the code, delete it — it will go stale and lie to the next
reader.

```python
# BAD - says nothing the code doesn't
i += 1                      # increment i

# GOOD - explains a decision you cannot see in the code
i += 1                      # skip the 'l.NN' line; it belongs to the error above
```

Write for someone who knows Python but has never seen LaTeX internals, batch
scripting, or this codebase.

---

## Layer 1 — File header

Every source file starts with a header saying what it is and what it is
responsible for. For non-obvious modules, add the design decisions a reader
would otherwise have to reverse-engineer.

```python
"""
log_parser.py - Turns raw LaTeX .log output into structured errors/warnings.

Two subtleties that a naive parser gets wrong:

* `-file-line-error` format. The compiler passes this flag, so errors appear as
  `main.tex:12: Undefined control sequence.` WITHOUT the leading `! `. Matching
  only "! " would silently drop the most common errors.
* Package warnings continue on lines starting with `(packagename)`, not a space.
"""
```

---

## Layer 2 — Section banners

Group related code and label the groups. This is what lets a reader (or an AI)
navigate a 600-line file without reading all of it.

```python
# ─── Binary resolution ────────────────────────────────────────────────────────
```

```css
/* ═══════════════════════════════════════════════════════════════
   FILE TREE - left panel, lists the uploaded project files
   ═══════════════════════════════════════════════════════════════ */
```

```javascript
// ─── Compile ──────────────────────────────────────────────────────────────────
```

Keep the banner style consistent within a file.

---

## Layer 3 — Function / class docstrings

Every public function, class and non-trivial helper gets one. Start with a
single-sentence summary in the imperative ("Return…", "Parse…", not "This
function returns…").

Add `Args:` / `Returns:` / `Raises:` only when they are not obvious from the
signature and type hints.

```python
def compile_project(workspace: Path, main_tex: str, engine: EngineType) -> CompilationResult:
    """Compile a LaTeX project and return a structured result.

    Tries latexmk first and falls back to manual passes ONLY when latexmk itself
    could not run (missing binary/Perl) - never merely because the document had
    errors. Build artifacts are cleaned first so `success` reflects THIS run and
    not a leftover PDF from a previous compile.
    """
```

Trivial dunders (`__init__` that only assigns, `__repr__`) may be skipped when
the class docstring already explains the fields.

**Tests are documentation.** Every test says what behaviour it proves, and — if
it is a regression test — what broke:

```python
def test_write18_blocked_by_default(self):
    """A document using \\write18 must not be able to run OS commands.

    Regression: shell-escape was once enabled unconditionally, so any uploaded
    .tex could execute arbitrary programs on the user's machine.
    """
```

---

## Layer 4 — Line comments

Use sparingly, for the things that would make a reader stop and frown:

- a magic number or constant (`79` is TeX's `max_print_line`)
- a workaround for someone else's bug (`tlmgr.bat always exits 0`)
- a security-relevant decision (`'p' = paranoid: confine file access`)
- a non-obvious ordering requirement (`must clean artifacts BEFORE compiling`)

```python
safe = re.split(r"[\\/]+", filename)[-1]   # split on BOTH separators: on POSIX,
                                           # Path().name ignores backslashes
```

---

## Naming

Names carry meaning; comments should not have to rescue a bad one.

| Use | Not |
|---|---|
| `session_dir` | `d` |
| `missing_files` | `mf`, `lst` |
| `raw_log`, `bib_log` | `raw`, `blg` |
| `match`, `group` | `m`, `g` |
| `is_fresh_pdf()` | `check()` |

Conventions:

- **Python** — `snake_case`; `_leading_underscore` for module-private helpers;
  `UPPER_CASE` for constants; `_RE_` prefix for compiled regexes.
- **JavaScript** — `camelCase`; `dom.*` for cached elements; `state.*` for app state.
- **CSS** — kebab-case, grouped by UI region (`.file-tree`, `.log-entry`).
- Booleans read as questions: `is_`, `has_`, `should_`, `allow_`.
- Short loop variables (`i`, `f`, `p`) are fine in a 1–2 line comprehension where
  the meaning is obvious from context. Anywhere longer, name them properly.

---

## Things that must NOT be renamed

These cross a boundary; renaming silently breaks the app:

- **JSON API field names** (`session_id`, `pdf_url`, `has_errors`, …) — the
  frontend reads them by name
- **HTML element ids** — `app.js` looks them up (`tests/test_frontend.py` guards this)
- **CSS class names** used in `app.js` or `index.html`
- **Environment variable names** (`LATEX_TIMEOUT`, …) — documented in the README
- **Function names asserted in tests** — rename the test in the same commit or don't rename

---

## Before you commit

```bash
.venv\Scripts\python.exe -m pytest      # all tests must pass
```

A documentation-only change must not alter behaviour. If a test fails after a
"comment-only" edit, you changed something real — find out what.
