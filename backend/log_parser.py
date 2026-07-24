"""
log_parser.py – Intelligent LaTeX compilation log parser.

Extracts structured errors, warnings, and informational messages from
the raw .log output produced by pdflatex/xelatex/lualatex.
"""

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class LogEntry:
    level: str          # "error" | "warning" | "info" | "badbox"
    message: str
    file: str = ""
    line: int | None = None
    context: str = ""   # Surrounding lines for context


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
        "level": e.level,
        "message": e.message,
        "file": e.file,
        "line": e.line,
        "context": e.context,
    }


# ─── Regex Patterns ───────────────────────────────────────────────────────────

# Hard errors: start with "! "
_RE_ERROR = re.compile(r"^!\s+(.+)$", re.MULTILINE)

# Line number associated with an error: "l.42 ..."
_RE_LINE_NUM = re.compile(r"^l\.(\d+)\s*(.*)", re.MULTILINE)

# LaTeX Warnings (package warnings, citation warnings, etc.)
_RE_WARNING = re.compile(
    r"^(LaTeX Warning|Package \w+ Warning|Class \w+ Warning|LaTeX Font Warning):\s*(.+?)(?=\n\n|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Citation warnings
_RE_CITE_WARNING = re.compile(
    r"(LaTeX Warning: Citation `[^']+' on page \d+ undefined)",
    re.MULTILINE,
)

# Reference warnings
_RE_REF_WARNING = re.compile(
    r"(LaTeX Warning: Reference `[^']+' on page \d+ undefined)",
    re.MULTILINE,
)

# Undefined references / multiply defined
_RE_UNDEF_WARNING = re.compile(
    r"(LaTeX Warning: There were undefined references)",
    re.MULTILINE,
)

# Missing file errors
_RE_MISSING_FILE = re.compile(
    r"(! LaTeX Error: File `([^']+)' not found\.|"
    r"! Package \w+ Error: Cannot find file `([^']+)'\.)",
    re.MULTILINE,
)

# Missing package
_RE_MISSING_PKG = re.compile(
    r"! LaTeX Error: File `([^']+)\.sty' not found",
    re.MULTILINE,
)

# Overfull / Underfull hbox
_RE_BADBOX = re.compile(
    r"^(Overfull|Underfull) \\[hv]box \(([^)]+)\) (?:in paragraph|in alignment|detected) at lines? (\d+)(?:--\d+)?",
    re.MULTILINE,
)

# Current file being processed: parenthesised paths
_RE_CURRENT_FILE = re.compile(r"\(([^()]+\.tex)\b")


# ─── Main Parser ──────────────────────────────────────────────────────────────

def parse_log(raw_log: str) -> ParsedLog:
    """
    Parse the raw LaTeX .log file content into structured log entries.
    """
    result = ParsedLog(raw=raw_log)
    lines = raw_log.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Hard Errors ──────────────────────────────────────────────────────
        if line.startswith("! "):
            error_msg = line[2:].strip()

            # Collect continuation lines (indented or continuation of error)
            context_lines = []
            j = i + 1
            line_num: int | None = None
            while j < len(lines) and j < i + 10:
                next_line = lines[j]
                ln_match = _RE_LINE_NUM.match(next_line)
                if ln_match:
                    line_num = int(ln_match.group(1))
                    context_lines.append(next_line)
                    j += 1
                    break
                context_lines.append(next_line)
                j += 1

            # Try to extract file context from surrounding lines
            current_file = _extract_current_file(lines, i)

            result.errors.append(LogEntry(
                level="error",
                message=error_msg,
                file=current_file,
                line=line_num,
                context="\n".join(context_lines[:5]),
            ))
            i = j
            continue

        # ── LaTeX / Package Warnings ─────────────────────────────────────────
        if any(line.startswith(prefix) for prefix in (
            "LaTeX Warning:", "Package ", "Class ", "LaTeX Font Warning:"
        )) and "Warning" in line:
            # Collect multi-line warning
            warning_lines = [line]
            j = i + 1
            while j < len(lines) and lines[j].startswith(" "):
                warning_lines.append(lines[j].strip())
                j += 1

            full_warning = " ".join(warning_lines)
            # Extract line number if present
            ln = None
            ln_search = re.search(r"on input line (\d+)", full_warning)
            if ln_search:
                ln = int(ln_search.group(1))

            current_file = _extract_current_file(lines, i)
            result.warnings.append(LogEntry(
                level="warning",
                message=full_warning.strip(),
                file=current_file,
                line=ln,
            ))
            i = j
            continue

        # ── Overfull / Underfull Hbox ─────────────────────────────────────────
        bb_match = _RE_BADBOX.match(line)
        if bb_match:
            result.badboxes.append(LogEntry(
                level="badbox",
                message=line.strip(),
                line=int(bb_match.group(3)),
            ))
            i += 1
            continue

        i += 1

    # ── Post-processing: deduplicate and detect common issues ─────────────────
    _detect_missing_packages(raw_log, result)
    _detect_bibliography_issues(raw_log, result)

    return result


def _extract_current_file(lines: List[str], error_idx: int) -> str:
    """
    Walk backwards from the error line to find the most recent file reference.
    """
    for k in range(error_idx, max(-1, error_idx - 30), -1):
        m = _RE_CURRENT_FILE.search(lines[k])
        if m:
            return m.group(1)
    return ""


def _detect_missing_packages(raw_log: str, result: ParsedLog) -> None:
    """Detect missing package errors and add friendly messages."""
    for m in _RE_MISSING_PKG.finditer(raw_log):
        pkg_name = m.group(1)
        # Avoid duplicate messages
        if not any(pkg_name in e.message for e in result.errors):
            result.errors.append(LogEntry(
                level="error",
                message=f"Missing LaTeX package: '{pkg_name}.sty'. "
                        f"Install it via MiKTeX Package Manager.",
            ))


def _detect_bibliography_issues(raw_log: str, result: ParsedLog) -> None:
    """Detect bibliography-related warnings."""
    if "No file" in raw_log and ".bbl" in raw_log:
        result.warnings.append(LogEntry(
            level="warning",
            message="Bibliography file (.bbl) not found. "
                    "Make sure your .bib file is uploaded and bibtex/biber ran successfully.",
        ))

    if "I found no" in raw_log and "\\citation" in raw_log:
        result.warnings.append(LogEntry(
            level="warning",
            message="BibTeX found no citations in the .aux file. "
                    "Check that \\cite{} commands exist in your .tex file.",
        ))


# ─── Summary Helper ───────────────────────────────────────────────────────────

def get_log_summary(parsed: ParsedLog) -> str:
    """Return a short human-readable summary of the log."""
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
