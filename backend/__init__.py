"""
LaTeX Studio – backend package.

A small FastAPI application that compiles uploaded LaTeX projects to PDF.

The single source of truth for the application version lives here so that
``run.py``, the FastAPI ``version=`` field and any About/health output all
report the same number.

Two reasons this file stays this small
--------------------------------------
* **No imports, ever.** ``run.py`` imports this package *before* it has checked
  that FastAPI and LaTeX are installed, so it must be importable on a broken
  install. Re-exporting submodules here would turn a missing dependency into a
  traceback instead of a readable ``[FAIL]`` line from the pre-flight check.
* **``scripts/install.ps1`` scrapes it.** The installer records the version in
  its manifest by matching the line below and splitting it on the double-quote
  character. So: keep the assignment on one line with a plain double-quoted
  literal, and do not let any other line in this file repeat that identifier —
  a second match silently turns the scraped version into a list.
"""

__version__ = "1.2.0"
