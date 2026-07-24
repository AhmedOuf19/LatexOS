"""
log_parser.py – Structured LaTeX log parser.

Turns the raw ``.log`` produced by pdflatex/xelatex/lualatex into structured
errors, warnings and over/underfull-box entries for the UI.

Two subtleties that a naive parser gets wrong and this one handles:

* **``-file-line-error`` format.** The compiler passes ``-file-line-error``, so
  the most common errors appear as ``main.tex:12: Undefined control sequence.``
  – WITHOUT the leading ``! ``. Matching only ``"! "`` would silently drop them,
  which would make a broken document look clean. We match both forms.
* **Package-warning continuations.** Package warnings continue on lines that
  begin with ``(packagename)`` rather than a space, so those continuation lines
  are collected too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class LogEntry:
    level: str          # "error" | "warning" | "info" | "badbox"
    message: str
    file: str = ""
    line: int | None = None
    context: str = ""   # surrounding lines, shown collapsed in the UI


@dataclass
class ParsedLog:
    errors: List[LogEntry] = field(default_factory=list)
    warnings: List[LogEntry] = field(default_factory=list)
    badboxes: List[LogEntry] = field(default_factory=list)
    info: List[LogEntry] = field(default_factory=list)
    raw: str = ""

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> dict:
        return {
            "has_errors": self.has_errors,
            "errors": [_entry_to_dict(e) for e in self.errors],
            "warnings": [_entry_to_dict(w) for w in self.warnings],
            "badboxes": [_entry_to_dict(b) for b in self.badboxes],
            "info": [_entry_to_dict(i) for i in self.info],
            "raw": self.raw,
        }


def _entry_to_dict(e: LogEntry) -> dict:
    return {
        "level": e.level, "message": e.message,
        "file": e.file, "line": e.line, "context": e.context,
    }


# ─── Regex Patterns ───────────────────────────────────────────────────────────

# "! Emergency stop." / "! Undefined control sequence." (no -file-line-error)
_RE_BANG_ERROR = re.compile(r"^!\s+(.+)$")

# "main.tex:12: Undefined control sequence." (with -file-line-error). The path
# may be preceded by "./" and contains no ':' before the line number.
_RE_FILELINE_ERROR = re.compile(r"^(?:\./)?([^:\n]+?):(\d+):\s*(.+)$")

# "l.42 \\badcommand" – the source line for a bang-style error.
_RE_LINE_NUM = re.compile(r"^l\.(\d+)\s*(.*)")

# Overfull / Underfull \hbox / \vbox … at lines 5--7
_RE_BADBOX = re.compile(
    r"^(Overfull|Underfull)\s+\\[hv]box\s+\(([^)]+)\)"
    r".*?(?:at lines?|has occurred while.*?lines?)\s+(\d+)"
)

# A file being opened, "(./chapters/chapter1.tex" – used to attribute errors.
_RE_FILE_OPEN = re.compile(r"\(([^()\s]+\.(?:tex|sty|cls|ltx))\b")

# Missing .sty package (for a friendly hint).
_RE_MISSING_PKG = re.compile(r"File `([^']+)\.sty' not found")


def _starts_warning(line: str) -> bool:
    """True if ``line`` begins a LaTeX/package/class/font warning."""
    return (
        line.startswith("LaTeX Warning:")
        or line.startswith("LaTeX Font Warning:")
        or (line.startswith(("Package ", "Class ")) and "Warning" in line)
    )


# ─── Main Parser ──────────────────────────────────────────────────────────────

def parse_log(raw_log: str) -> ParsedLog:
    """Parse raw ``.log`` text into a :class:`ParsedLog`."""
    result = ParsedLog(raw=raw_log)
    lines = _unwrap(raw_log.splitlines())

    # A parenthesis stack tracks the file currently being processed, so an error
    # is attributed to the most-recently-opened file rather than a guess.
    file_stack: List[str] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Keep the current-file stack roughly in sync with the log.
        _update_file_stack(line, file_stack)
        current_file = file_stack[-1] if file_stack else ""

        # ── file:line: error  (the -file-line-error format) ──────────────────
        fl = _RE_FILELINE_ERROR.match(line)
        if fl and _looks_like_error(fl.group(3)):
            result.errors.append(LogEntry(
                level="error",
                message=fl.group(3).strip(),
                file=fl.group(1).strip(),
                line=int(fl.group(2)),
                context="\n".join(lines[i + 1:i + 4]),
            ))
            i += 1
            continue

        # ── "! …" hard error ─────────────────────────────────────────────────
        bang = _RE_BANG_ERROR.match(line)
        if bang and not line.startswith("!  =="):
            message = bang.group(1).strip()
            context_lines: List[str] = []
            line_num: int | None = None
            j = i + 1
            while j < n and j < i + 12:
                ln = _RE_LINE_NUM.match(lines[j])
                if ln:
                    line_num = int(ln.group(1))
                    context_lines.append(lines[j])
                    j += 1
                    break
                context_lines.append(lines[j])
                j += 1
            result.errors.append(LogEntry(
                level="error", message=message, file=current_file,
                line=line_num, context="\n".join(context_lines[:5]),
            ))
            i = j
            continue

        # ── LaTeX / Package / Class / Font warning ───────────────────────────
        if _starts_warning(line):
            warning_lines = [line]
            j = i + 1
            # Continuations either start with a space (LaTeX) or with the
            # "(packagename)" marker (package warnings).
            while j < n and (
                lines[j].startswith(" ")
                or re.match(r"^\(\w[\w.-]*\)\s", lines[j])
            ):
                warning_lines.append(lines[j].strip())
                j += 1
            full = " ".join(warning_lines).strip()
            ln_search = re.search(r"on input line (\d+)", full)
            result.warnings.append(LogEntry(
                level="warning", message=full, file=current_file,
                line=int(ln_search.group(1)) if ln_search else None,
            ))
            i = j
            continue

        # ── Overfull / Underfull box ─────────────────────────────────────────
        bb = _RE_BADBOX.match(line)
        if bb:
            result.badboxes.append(LogEntry(
                level="badbox", message=line.strip(),
                line=int(bb.group(3)), file=current_file,
            ))
            i += 1
            continue

        i += 1

    _detect_missing_packages(raw_log, result)
    _detect_bibliography_issues(raw_log, result)
    _dedupe(result)
    return result


def _unwrap(lines: List[str]) -> List[str]:
    """Best-effort un-wrap of TeX's 79-column hard wrapping.

    TeX breaks long log lines at ``max_print_line`` (79). When a line is exactly
    79 characters we join the next line onto it, so a message or "on input line
    N" split across the wrap is not lost. Blank lines are never joined.
    """
    out: List[str] = []
    for line in lines:
        if out and len(out[-1]) == 79 and line and not line.startswith(" "):
            out[-1] += line
        else:
            out.append(line)
    return out


def _looks_like_error(message: str) -> bool:
    """Filter file:line: matches to real errors (avoid false positives on
    ordinary parenthesised paths that happen to contain a colon+digits)."""
    m = message.lower()
    return (
        "error" in m
        or "undefined" in m
        or "missing" in m
        or "runaway" in m
        or "not found" in m
        or "no such file" in m
        or "extra " in m
        or "already defined" in m
        or "emergency stop" in m
    )


def _update_file_stack(line: str, stack: List[str]) -> None:
    """Push newly-opened files and pop closed ones to track the current file."""
    idx = 0
    while idx < len(line):
        ch = line[idx]
        if ch == "(":
            m = _RE_FILE_OPEN.match(line, idx)
            if m:
                stack.append(m.group(1))
        elif ch == ")":
            if stack:
                stack.pop()
        idx += 1


def _detect_missing_packages(raw_log: str, result: ParsedLog) -> None:
    """Add a friendly hint for each missing .sty package."""
    for m in _RE_MISSING_PKG.finditer(raw_log):
        pkg_name = m.group(1)
        if not any(pkg_name in e.message for e in result.errors):
            result.errors.append(LogEntry(
                level="error",
                message=f"Missing LaTeX package: '{pkg_name}.sty'. "
                        f"It should install automatically; if not, install it "
                        f"with your LaTeX package manager.",
            ))


def _detect_bibliography_issues(raw_log: str, result: ParsedLog) -> None:
    """Surface common bibliography problems as warnings."""
    if "No file" in raw_log and ".bbl" in raw_log:
        result.warnings.append(LogEntry(
            level="warning",
            message="Bibliography file (.bbl) not found. Make sure your .bib "
                    "file is uploaded and bibtex/biber ran successfully.",
        ))
    if "I found no" in raw_log and r"\citation" in raw_log:
        result.warnings.append(LogEntry(
            level="warning",
            message="BibTeX found no citations in the .aux file. Check that "
                    r"\cite{} commands exist in your .tex file.",
        ))


def _dedupe(result: ParsedLog) -> None:
    """Collapse identical repeated entries (e.g. the same overfull box on every
    page) so the UI shows each distinct problem once."""
    for bucket_name in ("errors", "warnings", "badboxes", "info"):
        bucket = getattr(result, bucket_name)
        seen = set()
        unique: List[LogEntry] = []
        for e in bucket:
            key = (e.level, e.message, e.file, e.line)
            if key not in seen:
                seen.add(key)
                unique.append(e)
        setattr(result, bucket_name, unique)


# ─── Summary Helper ───────────────────────────────────────────────────────────

def get_log_summary(parsed: ParsedLog) -> str:
    """Short human-readable summary shown above the log panel."""
    parts = []
    if parsed.errors:
        parts.append(f"{len(parsed.errors)} error(s)")
    if parsed.warnings:
        parts.append(f"{len(parsed.warnings)} warning(s)")
    if parsed.badboxes:
        parts.append(f"{len(parsed.badboxes)} overfull/underfull box(es)")
    if not parts:
        return "Compilation completed with no issues."
    return "Compilation finished with: " + ", ".join(parts) + "."
