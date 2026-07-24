"""
conftest.py – shared pytest fixtures and configuration.

Two things every test in this suite relies on:

* **Isolation.** An autouse fixture redirects ``UPLOAD_DIR`` to a per-test temp
  directory, so tests never read or write the real ``uploads/`` folder and can
  never leak session directories or interfere with a running server.
* **A working ``requires_latex`` marker.** Tests that need a LaTeX distribution
  are marked ``@pytest.mark.requires_latex`` and are skipped automatically when
  no ``pdflatex`` is available – which is what the README's "runs without a
  LaTeX install" promise actually needs.
"""

from __future__ import annotations

import pytest

from backend.compiler import check_latex_available

# Detected once per session. Individual engine tests re-check their own binary.
_LATEX_STATUS = check_latex_available()
LATEX_AVAILABLE = _LATEX_STATUS.get("pdflatex", {}).get("available", False)


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    """Point every module's ``UPLOAD_DIR`` at a throwaway temp directory."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    import backend.config as config
    import backend.file_manager as file_manager
    import backend.main as main
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(file_manager, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    yield uploads


@pytest.fixture
def client():
    """A TestClient entered as a context manager, so the app lifespan (which
    mints the security token and starts background tasks) actually runs."""
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        yield c


def pytest_collection_modifyitems(config, items):
    """Skip @requires_latex tests when no LaTeX distribution is installed."""
    if LATEX_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="no LaTeX distribution installed (pdflatex not found)")
    for item in items:
        if "requires_latex" in item.keywords:
            item.add_marker(skip)
