"""
log_parser.py – Structured LaTeX log parser.

Turns the raw ``.log`` produced by pdflatex/xelatex/lualatex into structured
errors, warnings and over/underfull-box entries for the UI.

A TeX log is a stream of loosely formatted prose, not a machine-readable
format. Four of its habits break a naive parser; this module handles all four:

* **``-file-line-error`` format.** The compiler passes ``-file-line-error``, so
  the most common errors appear as ``main.tex:12: Undefined control sequence.``
  – WITHOUT the leading ``! ``. Matching only ``"! "`` would silently drop them,
  which would make a broken document look clean. We match both forms.
* **Package-warning continuations.** Package warnings continue on lines that
  begin with ``(packagename)`` rather than a space, so those continuation lines
  are collected too.
* **79-column hard wrapping.** TeX flushes its log every ``max_print_line``
  characters (79 by default), so one logical message can arrive split across
  several physical lines, often mid-word. ``_unwrap`` stitches those back
  together *before* any matching happens; otherwise a regex silently fails on a
  message that happened to be long.
* **Errors rarely name their own file.** A ``! …`` error says what went wrong
  but not where. TeX marks file boundaries only by printing ``(path`` when it
  opens a file and ``)`` when it closes it, so the parser mirrors that with a
  file stack and blames the innermost open file. Without it every error in an
  ``\\input`` chapter would be attributed to the main document.

Layout: one forward pass over the lines classifies errors / warnings / boxes,
then a set of ``_detect_*`` helpers re-scan the whole raw log for well-known
failure modes (missing package, shell-escape needed, hyperref bookmarks,
non-typesettable Unicode). Those hints exist because TeX's own wording is
accurate but unusable for a non-LaTeX author, who is this app's target user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    """One problem found in the log, in the shape the log panel renders.

    Some entries are lifted straight out of the log, others are hints this
    module writes itself (see the ``_detect_*`` helpers); the UI treats both
    identically, which is why a synthesised hint simply leaves ``file``/``line``
    empty rather than inventing a location.
    """
    level: str          # "error" | "warning" | "info" | "badbox"; app.js uses it
                        # verbatim as a CSS class and as the filter value
    message: str
    file: str = ""      # "" when the log never told us which file (see file stack)
    line: int | None = None
    context: str = ""   # surrounding lines, shown collapsed in the UI


@dataclass
class ParsedLog:
    """A whole compile log bucketed by severity, plus the untouched original.

    ``raw`` is carried along deliberately: this parser is best-effort on a
    format that has no specification, so the UI must still be able to show the
    user what TeX actually said when a failure is not recognised here.
    """
    errors: List[LogEntry] = field(default_factory=list)
    warnings: List[LogEntry] = field(default_factory=list)
    badboxes: List[LogEntry] = field(default_factory=list)
    info: List[LogEntry] = field(default_factory=list)
    raw: str = ""

    @property
    def has_errors(self) -> bool:
        """True if anything was classified as an error.

        Warnings and bad boxes deliberately do not count — an overfull ``\\hbox``
        is ugly but still produces a perfectly usable PDF, and flagging it as a
        failure would train users to ignore the error indicator.
        """
        return len(self.errors) > 0

    def to_dict(self) -> dict:
        """Return the JSON payload the frontend consumes.

        The key names below are the API contract: ``app.js`` reads them by name
        and ``tests/test_api.py`` asserts they exist, so renaming one breaks the
        log panel without any error being raised.
        """
        return {
            "has_errors": self.has_errors,
            "errors": [_entry_to_dict(e) for e in self.errors],
            "warnings": [_entry_to_dict(w) for w in self.warnings],
            "badboxes": [_entry_to_dict(b) for b in self.badboxes],
            "info": [_entry_to_dict(i) for i in self.info],
            "raw": self.raw,
        }


def _entry_to_dict(entry: LogEntry) -> dict:
    """Return ``entry`` as a plain JSON-serialisable dict.

    Spelled out by hand instead of ``dataclasses.asdict`` so the wire format
    stays pinned to exactly these five keys: a field added to :class:`LogEntry`
    for internal bookkeeping must not silently start leaking to the browser.
    """
    return {
        "level": entry.level, "message": entry.message,
        "file": entry.file, "line": entry.line, "context": entry.context,
    }


# ─── Regex Patterns ───────────────────────────────────────────────────────────

# "! Emergency stop." / "! Undefined control sequence." (no -file-line-error)
_RE_BANG_ERROR = re.compile(r"^!\s+(.+)$")

# "main.tex:12: Undefined control sequence." (with -file-line-error). The path
# may be preceded by "./" and may be a Windows absolute path with a drive
# letter (e.g. "c:/.../foo.sty:88: ...") — the optional "[A-Za-z]:" prefix keeps
# the leading drive-letter colon from ending the filename group.
_RE_FILELINE_ERROR = re.compile(r"^(?:\./)?((?:[A-Za-z]:)?[^:\n]+?):(\d+):\s*(.+)$")

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
    """Parse raw ``.log`` text into a :class:`ParsedLog`.

    The scan is index-driven rather than a ``for`` loop because an error or a
    warning owns the lines that follow it — its context dump or its wrapped
    continuation — and those lines must be *consumed*, not classified again as
    problems of their own.
    """
    result = ParsedLog(raw=raw_log)
    lines = _unwrap(raw_log.splitlines())

    # A parenthesis stack tracks the file currently being processed, so an error
    # is attributed to the most-recently-opened file rather than a guess.
    file_stack: List[str] = []

    index = 0
    total_lines = len(lines)
    while index < total_lines:
        line = lines[index]

        # Keep the current-file stack roughly in sync with the log. This has to
        # happen before the branches below, because TeX packs file openings and
        # message text onto the same line ("(./ch1.tex Overfull \hbox …").
        _update_file_stack(line, file_stack)
        current_file = file_stack[-1] if file_stack else ""

        # ── file:line: error  (the -file-line-error format) ──────────────────
        fileline_match = _RE_FILELINE_ERROR.match(line)
        if fileline_match and _looks_like_error(fileline_match.group(3)):
            result.errors.append(LogEntry(
                level="error",
                message=fileline_match.group(3).strip(),
                file=fileline_match.group(1).strip(),
                line=int(fileline_match.group(2)),
                context="\n".join(lines[index + 1:index + 4]),
            ))
            index += 1
            continue

        # ── "! …" hard error ─────────────────────────────────────────────────
        bang_match = _RE_BANG_ERROR.match(line)
        # "!  ==> Fatal error occurred, no output PDF file produced!" is TeX's
        # closing summary of an error already reported above, not a new problem.
        if bang_match and not line.startswith("!  =="):
            message = bang_match.group(1).strip()
            context_lines: List[str] = []
            line_num: int | None = None
            lookahead = index + 1
            # The source line ("l.42 \badcommand") follows the message a few
            # lines later. Cap the window at 12 lines so a runaway argument —
            # whose dump can run for hundreds of lines — cannot swallow the
            # next error.
            while lookahead < total_lines and lookahead < index + 12:
                line_num_match = _RE_LINE_NUM.match(lines[lookahead])
                if line_num_match:
                    line_num = int(line_num_match.group(1))
                    context_lines.append(lines[lookahead])
                    lookahead += 1
                    break               # "l.NN" is the last line of this error
                context_lines.append(lines[lookahead])
                lookahead += 1
            result.errors.append(LogEntry(
                level="error", message=message, file=current_file,
                line=line_num, context="\n".join(context_lines[:5]),
            ))
            index = lookahead           # skip the lines consumed as context
            continue

        # ── LaTeX / Package / Class / Font warning ───────────────────────────
        if _starts_warning(line):
            warning_lines = [line]
            lookahead = index + 1
            # Continuations either start with a space (LaTeX) or with the
            # "(packagename)" marker (package warnings).
            while lookahead < total_lines and (
                lines[lookahead].startswith(" ")
                or re.match(r"^\(\w[\w.-]*\)\s", lines[lookahead])
            ):
                warning_lines.append(lines[lookahead].strip())
                lookahead += 1
            full_message = " ".join(warning_lines).strip()
            # Search the joined text, not the first line: the 79-column wrap
            # regularly pushes "on input line NN" onto a continuation line.
            input_line_match = re.search(r"on input line (\d+)", full_message)
            result.warnings.append(LogEntry(
                level="warning", message=full_message, file=current_file,
                line=int(input_line_match.group(1)) if input_line_match else None,
            ))
            index = lookahead
            continue

        # ── Overfull / Underfull box ─────────────────────────────────────────
        badbox_match = _RE_BADBOX.match(line)
        if badbox_match:
            result.badboxes.append(LogEntry(
                level="badbox", message=line.strip(),
                line=int(badbox_match.group(3)), file=current_file,
            ))
            index += 1
            continue

        index += 1

    # Whole-log passes: these spot failure modes the line scan cannot see and
    # append plain-English advice. _dedupe runs last so it also collapses
    # duplicates they produce (the same missing package reported once per pass).
    _detect_missing_packages(raw_log, result)
    _detect_bibliography_issues(raw_log, result)
    _detect_shell_escape_needed(raw_log, result)
    _detect_bookmark_problem(raw_log, result)
    _detect_unicode_problem(raw_log, result)
    _dedupe(result)
    return result


# ─── Line-level helpers ───────────────────────────────────────────────────────

def _starts_new_record(line: str) -> bool:
    """True if ``line`` begins a new log record and so must NOT be merged onto
    the previous line by the 79-column un-wrapper."""
    return (
        line.startswith(("!", "l.", "Overfull", "Underfull",
                         "LaTeX Warning", "LaTeX Font Warning",
                         "Package ", "Class "))
        or bool(_RE_FILELINE_ERROR.match(line))
    )


def _unwrap(lines: List[str]) -> List[str]:
    """Best-effort un-wrap of TeX's 79-column hard wrapping.

    TeX breaks long log lines at ``max_print_line`` (79). We rejoin a
    continuation when the accumulated line length is a positive multiple of 79
    (so 3+ segments coalesce, not just the first) AND the next line does not
    itself start a new error/warning record (so a line that happens to be
    exactly 79 chars is not glued to a following ``! …`` error). Blank lines are
    never joined.
    """
    out: List[str] = []
    for line in lines:
        prev = out[-1] if out else ""
        if (prev and len(prev) % 79 == 0 and line
                and not line.startswith(" ")
                and not _starts_new_record(line)):
            out[-1] = prev + line
        else:
            out.append(line)
    return out


# A file:line: line that is actually a warning/box/info (not an error). In
# practice TeX rewrites only real errors into the file:line: form, but we guard
# defensively so a stray match is not mis-reported as an error.
_RE_BENIGN_FILELINE = re.compile(
    r"^(?:(?:LaTeX|Package|Class)\b.*\bWarning\b|Overfull\b|Underfull\b|Warning\b|Info\b)",
    re.IGNORECASE,
)


def _looks_like_error(message: str) -> bool:
    """Decide whether a ``file:line: message`` line is a real error.

    Because the compiler runs with ``-file-line-error``, TeX prints every hard
    error (undefined control sequence, "Too many }'s", "Double superscript",
    "Illegal unit of measure", "Misplaced alignment tab", …) in the
    ``file:line:`` form WITHOUT the leading ``! ``. So we treat any such line as
    an error unless it is clearly a warning/box/info line — rather than relying
    on a hand-maintained substring whitelist that silently dropped many real
    errors.
    """
    return not _RE_BENIGN_FILELINE.match(message.strip())


def _update_file_stack(line: str, stack: List[str]) -> None:
    """Push newly-opened files and pop closed ones to track the current file.

    TeX announces the file it is reading as ``(path`` and its end as ``)``,
    several of them per line and interleaved with ordinary message text — this
    is the only location information most errors ever get. Scanning character by
    character (rather than one regex per line) is what lets nested opens and
    closes on the same line be applied in order.

    Attribution is best-effort by nature: parentheses inside messages and file
    names unbalance the stack, so a wrong ``file`` on an entry is cosmetic and
    must never be used for a correctness decision.
    """
    idx = 0
    while idx < len(line):
        ch = line[idx]
        if ch == "(":
            open_match = _RE_FILE_OPEN.match(line, idx)
            if open_match:
                stack.append(open_match.group(1))
        elif ch == ")":
            if stack:                   # a stray ")" from prose must not crash
                stack.pop()
        idx += 1


# ─── Whole-log detectors (plain-English hints) ────────────────────────────────

def _detect_missing_packages(raw_log: str, result: ParsedLog) -> None:
    """Add a friendly hint for each missing .sty package.

    TinyTeX installs packages on demand, so a missing .sty usually means the
    auto-install did not happen (offline, or the package does not exist) — the
    raw ``File 'x.sty' not found`` says nothing about what the user should do.
    """
    for match in _RE_MISSING_PKG.finditer(raw_log):
        pkg_name = match.group(1)
        # Skip when the line scan already reported this package, so the user is
        # not shown the same missing package twice with different wording.
        if not any(pkg_name in e.message for e in result.errors):
            result.errors.append(LogEntry(
                level="error",
                message=f"Missing LaTeX package: '{pkg_name}.sty'. "
                        f"It should install automatically; if not, install it "
                        f"with your LaTeX package manager.",
            ))


_RE_NO_BBL = re.compile(r"No file .*\.bbl\b")
_RE_NO_CITATION = re.compile(r"I found no \\?citation", re.IGNORECASE)


def _detect_bibliography_issues(raw_log: str, result: ParsedLog) -> None:
    """Surface common bibliography problems as warnings.

    Both checks match a *single* line (via regex), not two unrelated substrings
    anywhere in the log — otherwise a successful build that loads ``(./main.bbl)``
    and also has a benign ``No file main.out.`` line would wrongly report a
    missing bibliography.
    """
    if _RE_NO_BBL.search(raw_log):
        result.warnings.append(LogEntry(
            level="warning",
            message="Bibliography file (.bbl) not found. Make sure your .bib "
                    "file is uploaded and bibtex/biber ran successfully.",
        ))
    if _RE_NO_CITATION.search(raw_log):
        result.warnings.append(LogEntry(
            level="warning",
            message="BibTeX found no citations in the .aux file. Check that "
                    r"\cite{} commands exist in your .tex file.",
        ))


# Signs that the document ACTUALLY FAILED for want of shell-escape.
#
# Deliberately narrow. Several packages merely *mention* shell-escape while
# working perfectly - epstopdf, for instance, emits
#
#     Package epstopdf Warning: Shell escape feature is not enabled.
#
# on every document that loads graphicx, purely to say it could also convert EPS
# if it were on. Treating that as an error made the app cry wolf on documents
# that compiled flawlessly, which is worse than saying nothing: a user who learns
# to ignore the error panel will ignore the real errors too. So we match only the
# messages that mean a package gave up.
_RE_SHELL_ESCAPE_NEEDED = re.compile(
    r"minted executable is unavailable or disabled"
    r"|You must invoke LaTeX with the -shell-escape flag"
    r"|\\write18 is disabled"
    r"|runsystem\([^)]*\)\.{3}disabled",
    re.IGNORECASE,
)

# Packages that name shell-escape only as an aside. If a hit came from one of
# these lines, it is informational, not a failure.
_RE_SHELL_ESCAPE_ADVISORY = re.compile(
    r"Package epstopdf Warning: Shell escape feature is not enabled",
    re.IGNORECASE,
)


def _detect_shell_escape_needed(raw_log: str, result: ParsedLog) -> None:
    """Tell the user exactly how to fix a genuine shell-escape failure.

    Packages like minted have to run an external program, which is blocked
    unless shell-escape is enabled. The raw LaTeX message never says how to
    enable it, so we add one actionable entry.

    Advisory mentions (see ``_RE_SHELL_ESCAPE_ADVISORY``) are ignored: they
    appear on perfectly healthy documents and an error the user cannot act on
    only teaches them to distrust the error panel.
    """
    for line in raw_log.splitlines():
        if _RE_SHELL_ESCAPE_ADVISORY.search(line):
            continue
        if _RE_SHELL_ESCAPE_NEEDED.search(line):
            result.errors.append(LogEntry(
                level="error",
                message="This document needs shell-escape (minted / svg / gnuplot). "
                        "Tick the 'Shell-escape' box next to the Compile button and "
                        "compile again. Only enable it for documents you trust - it "
                        "lets the document run programs on your computer.",
            ))
            return


# hyperref bookmark broken by a fragile command in a section title.
_RE_BOOKMARK_RUNAWAY = re.compile(
    r"File ended while scanning use of \\@@BOOKMARK|Runaway argument.*BOOKMARK",
    re.DOTALL,
)


def _detect_bookmark_problem(raw_log: str, result: ParsedLog) -> None:
    """Explain the classic hyperref \\@@BOOKMARK runaway error.

    It means a section/chapter title contains something hyperref cannot put in a
    PDF bookmark – most often \\url{...} or a raw underscore.
    """
    if _RE_BOOKMARK_RUNAWAY.search(raw_log):
        result.errors.append(LogEntry(
            level="error",
            message="A section/chapter title contains something hyperref cannot put "
                    "in a PDF bookmark - most often \\url{...} (it emits a raw % "
                    "that comments out the rest of the bookmark file). Replace it in "
                    "the TITLE with plain text, e.g. "
                    "\\section{Spec of \\texttt{MY\\_NAME}} instead of "
                    "\\section{Spec of \\url{MY_NAME}}, or wrap it with "
                    "\\texorpdfstring{...}{...}. Check every title, not just the first.",
        ))


# pdflatex cannot typeset arbitrary Unicode (Greek letters, subscripts, …).
_RE_UNICODE_CHAR = re.compile(r"Unicode character (.*?) \(U\+([0-9A-Fa-f]+)\)")


def _detect_unicode_problem(raw_log: str, result: ParsedLog) -> None:
    """Explain "Unicode character ... not set up for use with LaTeX".

    This happens with pdflatex, which only understands a limited character set.
    Switching the engine to xelatex or lualatex fixes it outright, so say so.

    The wording deliberately names the *engine*, not a place to click: this
    message is read both in the web UI (which has an engine selector) and by
    CLI/agent callers, where "top right" would be meaningless.
    """
    chars = {m.group(2).upper() for m in _RE_UNICODE_CHAR.finditer(raw_log)}
    if not chars:
        return
    sample = ", ".join(f"U+{c}" for c in sorted(chars)[:6])
    result.errors.append(LogEntry(
        level="error",
        message=f"Your document uses Unicode characters ({sample}) that pdflatex "
                f"cannot typeset. Easiest fix: compile with the xelatex or "
                f"lualatex engine instead, which support Unicode directly. "
                f"Alternatively replace them with LaTeX commands, "
                f"e.g. $\\theta$ instead of a literal Greek theta.",
    ))


def _dedupe(result: ParsedLog) -> None:
    """Collapse identical repeated entries (e.g. the same overfull box on every
    page) so the UI shows each distinct problem once.

    ``context`` is intentionally left out of the identity key: latexmk runs the
    document two or three times, and the same problem can come back with a
    slightly different context dump each pass while being one problem.
    """
    for bucket_name in ("errors", "warnings", "badboxes", "info"):
        bucket = getattr(result, bucket_name)
        seen = set()
        unique: List[LogEntry] = []
        for entry in bucket:
            key = (entry.level, entry.message, entry.file, entry.line)
            if key not in seen:
                seen.add(key)
                unique.append(entry)
        setattr(result, bucket_name, unique)


# ─── Summary Helper ───────────────────────────────────────────────────────────

def get_log_summary(parsed: ParsedLog) -> str:
    """Short human-readable summary shown above the log panel.

    Becomes the ``summary`` field of the compile response; ``compiler.py``
    prefixes it with the failure text when no PDF was produced, so the wording
    here stays neutral about success and only reports what was counted.
    """
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
