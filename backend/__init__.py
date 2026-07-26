"""
LaTeX Studio – backend package.

A small FastAPI application that compiles uploaded LaTeX projects to PDF.

The single source of truth for the application version lives here so that
``run.py``, the FastAPI ``version=`` field and any About/health output all
report the same number.
"""

__version__ = "1.1.2"
