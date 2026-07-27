"""
test_invariants.py - tripwires for security invariants nothing else asserts.

Every test here exists because a well-meaning pull request could otherwise
weaken a security property AND still see a completely green CI run. They are
deliberately blunt: they assert the *default* value, not the behaviour, because
the danger is a one-line change to a default.

If one of these fails, the fix is almost always to change your code back - not
to change the test. If you are deliberately changing an invariant, say so in the
pull request and explain why it is still safe (see CONTRIBUTING.md).

Kept free of any LaTeX dependency so it runs in the fast PR job, not just the
nightly one.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

import backend.config as config
import backend.file_manager as fm

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Network exposure ─────────────────────────────────────────────────────────

def test_binds_loopback_by_default():
    """The app has NO authentication. Binding anything but loopback exposes an
    unauthenticated LaTeX compiler to the network."""
    assert config.HOST == "127.0.0.1", (
        "HOST must default to 127.0.0.1. This app has no authentication; "
        "binding 0.0.0.0 exposes an arbitrary-code-execution surface."
    )


def test_trusted_hosts_not_wildcarded():
    """TrustedHostMiddleware blunts DNS-rebinding attacks against the loopback
    service; a '*' entry disables that protection entirely."""
    from backend.main import app

    for mw in app.user_middleware:
        hosts = mw.kwargs.get("allowed_hosts") if hasattr(mw, "kwargs") else None
        if hosts:
            assert "*" not in hosts, "allowed_hosts must not contain '*'"


# ── Shell escape (arbitrary command execution) ───────────────────────────────

def test_shell_escape_config_default_off():
    """\\write18 lets a .tex run OS commands. It must be opt-in."""
    assert config.ALLOW_SHELL_ESCAPE is False, (
        "ALLOW_SHELL_ESCAPE must default to False - it allows a document to "
        "run arbitrary programs on the user's computer."
    )


def test_compile_endpoint_shell_escape_defaults_false():
    """The API's own default must be False too.

    The compiler-level tests cannot see this: if the /api/compile form default
    flipped to True, every compile would silently gain shell-escape.
    """
    import inspect

    from backend.main import compile_latex

    param = inspect.signature(compile_latex).parameters["shell_escape"]
    default = param.default
    # FastAPI wraps the default in a Form(...) object; unwrap it if needed.
    actual = getattr(default, "default", default)
    assert actual is False, "/api/compile must default shell_escape to False"


def test_texmfoutput_and_openany_confine_file_access():
    """openin_any/openout_any = 'p' stop \\input{C:/…/secret} exfiltration."""
    from backend.compiler import _build_env

    env = _build_env(REPO_ROOT)
    assert env["openin_any"] == "p"
    assert env["openout_any"] == "p"
    assert env["TEXMFOUTPUT"] == str(REPO_ROOT)


# ── Upload / archive limits ──────────────────────────────────────────────────

def test_extension_whitelist_has_no_dangerous_entries():
    """An empty suffix re-opens the .latexmkrc RCE (latexmk executes it), and
    script extensions have no business in a LaTeX project."""
    banned = {"", ".ps1", ".bat", ".cmd", ".sh", ".py", ".exe", ".dll", ".com"}
    overlap = banned & config.ALLOWED_EXTENSIONS
    assert not overlap, f"dangerous extensions must not be whitelisted: {overlap}"


def test_zip_member_count_is_capped():
    """A ZIP with a huge number of tiny members is a cheap resource attack; the
    cap in _extract_zip had no test at all."""
    assert config.MAX_ZIP_MEMBERS > 0

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(config.MAX_ZIP_MEMBERS + 5):
            zf.writestr(f"f{i}.tex", "x")

    with pytest.raises(HTTPException) as exc:
        fm._extract_zip(buf.getvalue(), REPO_ROOT / "does_not_matter")
    assert exc.value.status_code == 400


def test_size_caps_are_positive():
    """A cap of 0/None would disable the limit rather than tighten it."""
    assert config.MAX_UPLOAD_SIZE_BYTES > 0
    assert config.MAX_EXTRACTED_SIZE_BYTES > 0
    assert config.MAX_LOG_READ_BYTES > 0


# ── CI workflow ──────────────────────────────────────────────────────────────

def test_workflows_are_not_pull_request_target():
    """`pull_request_target` runs with repository secrets and a writable token
    in the context of the BASE repo. Combined with checking out the PR's code it
    is a classic full-repo-compromise vector. Plain `pull_request` is correct.
    """
    yaml = pytest.importorskip("yaml")
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        data = yaml.safe_load(wf.read_text(encoding="utf-8"))
        # 'on' is parsed by YAML as the boolean True
        triggers = data.get("on", data.get(True, {})) or {}
        if isinstance(triggers, dict):
            assert "pull_request_target" not in triggers, (
                f"{wf.name} must not use pull_request_target - it exposes "
                f"secrets and a writable token to fork PRs."
            )


def test_workflows_declare_least_privilege_permissions():
    """Without an explicit permissions block the workflow inherits the repo
    default, which on older repos is a read/write GITHUB_TOKEN."""
    yaml = pytest.importorskip("yaml")
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        data = yaml.safe_load(wf.read_text(encoding="utf-8"))
        perms = data.get("permissions")
        assert perms is not None, f"{wf.name} must declare a permissions block"
        assert perms.get("contents") == "read", (
            f"{wf.name} should use 'contents: read' unless it genuinely needs "
            f"to write - say why in the PR if you change this."
        )
