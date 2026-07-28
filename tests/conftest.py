"""
conftest.py – shared pytest fixtures and configuration.

Three things every test in this suite relies on:

* **Isolation.** An autouse fixture redirects ``UPLOAD_DIR`` to a per-test temp
  directory, so tests never read or write the real ``uploads/`` folder and can
  never leak session directories or interfere with a running server.
* **A working ``requires_latex`` marker.** Tests that need a LaTeX distribution
  are marked ``@pytest.mark.requires_latex`` and are skipped automatically when
  no ``pdflatex`` is available – which is what the README's "runs without a
  LaTeX install" promise actually needs. The skip is deliberately *not* the whole
  story: see ``REQUIRE_LATEX`` below for the escape hatch that stops the skip from
  hiding a broken CI job.
* **A properly started app.** The ``client`` fixture enters TestClient as a
  context manager, because several endpoints only work once the lifespan has run.

Being in ``tests/conftest.py`` means pytest imports this before collection and
applies it to every test module automatically – nothing here needs importing.
"""

from __future__ import annotations

import os

import pytest

from backend.compiler import check_latex_available

# ─── LaTeX availability ───────────────────────────────────────────────────────
# Detected once per session. Individual engine tests re-check their own binary.
_LATEX_STATUS = check_latex_available()
LATEX_AVAILABLE = _LATEX_STATUS.get("pdflatex", {}).get("available", False)

# Set to 1 in the nightly CI job. It turns "no LaTeX -> skip" into a hard error,
# so a broken TeX install can never make the end-to-end security tests (notably
# the live \write18 regression) silently vanish from a green run.
# Parsed leniently because CI systems and shells spell booleans inconsistently.
REQUIRE_LATEX = os.getenv("LATEX_STUDIO_REQUIRE_LATEX", "").strip().lower() in {
    "1", "true", "yes", "on"
}


# ─── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    """Point every module's ``UPLOAD_DIR`` at a throwaway temp directory.

    Autouse and unconditional: a test that forgot to opt in would otherwise write
    real session folders into the user's ``uploads/``, and a cleanup test could
    delete work belonging to a server running alongside the suite.

    All three modules must be patched separately. ``file_manager`` and ``main``
    do ``from backend.config import UPLOAD_DIR``, which binds the value into their
    own namespaces at import time – patching ``config`` alone would leave them
    pointing at the real directory.
    """
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
    """Yield a TestClient entered as a context manager, so the app lifespan (which
    mints the security token and starts background tasks) actually runs.

    Constructing ``TestClient(app)`` without ``with`` skips startup entirely, and
    every authenticated request would then fail against a token that was never
    generated. The imports are function-local so that merely collecting the suite
    does not drag in FastAPI's test client and the whole app for the many test
    modules that never ask for a client.
    """
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        yield c


# ─── Collection hooks ─────────────────────────────────────────────────────────
def pytest_collection_modifyitems(config, items):
    """Skip @requires_latex tests when no LaTeX distribution is installed.

    Unless ``LATEX_STUDIO_REQUIRE_LATEX=1`` is set (the nightly CI job does),
    in which case a missing LaTeX install is a hard failure. Otherwise a broken
    TinyTeX install would let the end-to-end ``\\write18`` security regression
    quietly stop running while CI stayed green.

    Raising ``UsageError`` rather than failing the individual tests is deliberate:
    the problem is the environment, not the code, and it should stop the run
    immediately instead of producing a wall of identical failures.
    """
    if LATEX_AVAILABLE:
        return
    if REQUIRE_LATEX:
        raise pytest.UsageError(
            "LATEX_STUDIO_REQUIRE_LATEX=1 but pdflatex was not found. The "
            "@requires_latex security tests (including the live \\write18 "
            "regression) would have been silently skipped. Fix the LaTeX "
            "install in this job before trusting the result."
        )
    skip = pytest.mark.skip(reason="no LaTeX distribution installed (pdflatex not found)")
    for item in items:
        if "requires_latex" in item.keywords:
            item.add_marker(skip)
