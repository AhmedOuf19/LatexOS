"""
main.py – FastAPI application: routes, security middleware, and lifespan.

Security model (this app has NO authentication and is meant for local use)
--------------------------------------------------------------------------
Because there are no user accounts, *reachability is the authorization
boundary*: whoever can send a request to the server is allowed to use it. Three
layers keep that boundary where the user expects it – on their own machine.

1. **Loopback bind.** The server listens on 127.0.0.1 by default (see
   ``backend/config.HOST``, asserted in tests/test_invariants.py) so nothing on
   the local network or the internet can reach it at all. Everything below only
   matters for attacks that come *through the user's own browser*, which is the
   one client that can already reach loopback.
2. **Origin check.** Any web page the user visits can issue requests to
   ``http://127.0.0.1:8000`` – that is a normal, allowed browser behaviour, and
   it is how "drive-by localhost" attacks work: a random site silently uploads a
   .tex and compiles it on the victim's machine. Browsers do, however, attach an
   ``Origin`` header to such cross-origin requests. Since the frontend is served
   *same-origin* by this same app, no legitimate browser request ever carries a
   foreign Origin, so ``enforce_local_access`` can reject them outright.
3. **Per-instance token.** ``index.html`` is served with a fresh random token
   substituted in, and the frontend echoes it as ``X-Studio-Token``. Only
   same-origin scripts can read the page, so an attacker cannot obtain it. This
   is defence in depth for the case where the Origin header is absent or
   spoofable.

Deliberate gap: non-browser clients (tests, curl, editor plugins) send neither
an Origin nor a token, and are allowed through – for them the loopback bind is
the only boundary, and that is the intended trade-off for a local dev tool.
See also SECURITY.md.

Concurrency
-----------
A compile is a blocking subprocess that can legitimately run for minutes
(``COMPILE_TIMEOUT`` defaults to 600s). Calling it directly from an ``async``
route would occupy the single event-loop thread for that whole time and freeze
every other request – including the browser's own status polls and the PDF
fetch. Compiles are therefore dispatched with ``run_in_threadpool``.

Per-session state
-----------------
``_last_pdf`` / ``_last_log`` remember the exact artifact each compile produced.
The alternative – globbing the session folder for the newest ``.pdf`` – is
wrong in two ways: a project can contain several .tex files (and therefore
several PDFs), and a PDF that could not be deleted before the compile (locked by
a viewer or an AV scanner) would make a *failed* compile look successful. Naming
the artifact removes the guesswork.

REST API
--------
  POST   /api/upload            – upload project files, get a session_id
  POST   /api/compile           – compile, get structured log + pdf_url
  GET    /api/pdf/{session}      – stream the compiled PDF (?download=1 to save)
  GET    /api/log/{session}      – raw or ?parsed=true structured log
  GET    /api/files/{session}    – list files
  GET    /api/files/{session}/{path} – read one file (text or image)
  PUT    /api/files/{session}/{path} – write one file
  GET    /api/status            – LaTeX tool availability
  DELETE /api/cleanup/{session} – remove a session workspace
  GET    /                      – the frontend
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend import __version__
from backend.compiler import check_latex_available, compile_project
from backend.config import (
    ALLOW_SHELL_ESCAPE,
    COMPILE_TIMEOUT,
    DEFAULT_ENGINE,
    ENGINES,
    FRONTEND_DIR,
    LOG_LEVEL,
    MAX_UPLOAD_SIZE_BYTES,
    MAX_UPLOAD_SIZE_MB,
    SESSION_GC_INTERVAL_SECONDS,
    UPLOAD_DIR,
)
from backend.file_manager import (
    cleanup_stale_sessions,
    create_session,
    delete_session,
    detect_main_tex,
    get_session_dir,
    is_valid_session_id,
    list_session_files,
    read_file_content,
    save_uploaded_files,
    touch_session,
    write_file_content,
)
from backend.log_parser import parse_log

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("latex_studio")

# ─── Trusted origins ─────────────────────────────────────────────────────────
# An Origin is trusted only if it is a localhost/loopback origin. The port is
# deliberately unconstrained: the user may start the server on any port, and any
# port on loopback is equally "this machine".
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")

# One component of a static asset path (e.g. "vendor", "app.js"). Deliberately an
# allow-list of letters, digits, dot, dash and underscore, and it must contain at
# least one character that is NOT a dot - so "." and ".." are rejected, while
# "app.js" and ".gitkeep" are fine. A component cannot therefore be an absolute
# path, a "C:" drive prefix or an NTFS stream name ("file.js:stream"): those are
# refused before any path is built, rather than normalised away afterwards.
_SAFE_PATH_COMPONENT = re.compile(r"(?=.*[A-Za-z0-9_-])[A-Za-z0-9._-]+")


# ─── In-memory session state ─────────────────────────────────────────────────
# Plain module-level dicts are safe here only because the app is single-process
# and single-user by design (one uvicorn worker – see run.py). Anything that
# must survive a restart is recovered from disk by _find_artifact_on_disk().
#
#   _last_pdf/_last_log – the exact artifact a compile produced, so /api/pdf and
#                         /api/log serve THAT file rather than guessing by mtime
#                         (see "Per-session state" in the module docstring).
#   _compiling          – sessions with a compile in flight, to reject overlap
#                         and to refuse deletion mid-build.
# Entries are evicted by _session_gc_loop() once the workspace is gone, so the
# maps cannot grow without bound.
_last_pdf: Dict[str, Path] = {}
_last_log: Dict[str, Path] = {}
_compiling: set[str] = set()


# ─── Artifact recovery ───────────────────────────────────────────────────────

def _find_artifact_on_disk(session_dir: Path, ext: str) -> Path | None:
    """Locate the compiled ``ext`` artifact (.pdf/.log) for a session's main
    document on disk.

    This is the recovery path for when the in-memory ``_last_pdf``/``_last_log``
    entry is missing – chiefly after a server restart, where the maps start empty
    but a browser tab is still open on a session whose PDF is sitting on disk.
    Guessing is acceptable here (unlike in the normal path) because the
    alternative is a spurious 404 on work the user can see was compiled.
    """
    main_tex = detect_main_tex(session_dir)
    if main_tex:
        candidate = (session_dir / main_tex).with_suffix(ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    # Fallback: newest matching artifact. The sibling-.tex requirement keeps a
    # user-*uploaded* PDF (an included figure, say) from being served as though
    # it were the compiled output; the size check skips truncated/aborted writes.
    matches = [p for p in session_dir.rglob(f"*{ext}")
               if p.stat().st_size > 0 and p.with_suffix(".tex").exists()]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: prepare dirs, mint the instance token, log LaTeX status, and
    start the background session cleaner. Shutdown: stop the cleaner."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # A fresh, unguessable token per server start. Minting it here rather than
    # persisting it means a token that somehow leaked (a screenshot, a shared
    # log) stops working the next time the user launches the app.
    app.state.token = secrets.token_urlsafe(32)

    deleted = cleanup_stale_sessions()
    if deleted:
        logger.info(f"Cleaned up {deleted} stale session(s) on startup.")

    latex_status = check_latex_available()
    available_tools = [name for name, info in latex_status.items() if info["available"]]
    missing_tools = [name for name, info in latex_status.items() if not info["available"]]
    if available_tools:
        logger.info(f"LaTeX tools available: {', '.join(available_tools)}")
    if missing_tools:
        logger.warning(f"LaTeX tools NOT found: {', '.join(missing_tools)}")
    # Surfaced at startup rather than at the first compile: a user with no LaTeX
    # installed should learn it from the launcher window, not from a failed build.
    if not latex_status.get("pdflatex", {}).get("available"):
        logger.error("pdflatex is NOT available – compilation will fail until a "
                     "LaTeX distribution is installed.")
    if ALLOW_SHELL_ESCAPE:
        logger.warning("SHELL-ESCAPE IS ENABLED (LATEX_ALLOW_SHELL_ESCAPE=1). "
                       "Only compile documents you trust.")

    # Periodic cleanup so a long-running server does not accumulate sessions.
    gc_task = None
    if SESSION_GC_INTERVAL_SECONDS > 0:
        gc_task = asyncio.create_task(_session_gc_loop())

    yield

    if gc_task:
        gc_task.cancel()
    logger.info("Application shutting down.")


async def _session_gc_loop() -> None:
    """Sweep stale sessions on an interval until cancelled, and evict the
    in-memory PDF/log entries for sessions whose workspace no longer exists (so
    the maps cannot grow without bound on a long-running server)."""
    while True:
        await asyncio.sleep(SESSION_GC_INTERVAL_SECONDS)
        try:
            # Recursive directory deletion is blocking disk I/O; keep it off the
            # event loop for the same reason compiles are (see /api/compile).
            removed = await run_in_threadpool(cleanup_stale_sessions)
            # Snapshot the keys first – popping while iterating the dict itself
            # would raise RuntimeError.
            for sid in [s for s in _last_pdf if not (UPLOAD_DIR / s).exists()]:
                _last_pdf.pop(sid, None)
            for sid in [s for s in _last_log if not (UPLOAD_DIR / s).exists()]:
                _last_log.pop(sid, None)
            if removed:
                logger.info(f"Background cleanup removed {removed} stale session(s).")
        except Exception as exc:
            # Swallow everything: one bad sweep (a file locked by a PDF viewer or
            # an AV scanner on Windows is the usual cause) must not kill the task
            # and silently stop all future cleanups for the server's lifetime.
            logger.warning(f"Session cleanup error: {exc}")


# ─── App instance ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="LaTeX Studio",
    description="Upload LaTeX project files and compile them to PDF in the browser.",
    version=__version__,
    lifespan=lifespan,
)


# ─── Security middleware ─────────────────────────────────────────────────────
# See the module docstring for the full threat model. Together these two
# middlewares are what separates "a local tool" from "an API any web page can
# drive".

# Blocks DNS rebinding, the one attack the Origin check below cannot see: the
# victim loads evil.com, the attacker re-points that name at 127.0.0.1, and the
# browser now considers requests to evil.com:8000 *same-origin* – a same-origin
# GET carries no Origin header at all, so there is nothing for the Origin check
# to reject. The Host header still reads "evil.com", and this middleware refuses
# any Host that is not a loopback name.
# "testserver" is the Host that Starlette's TestClient sends; without it the
# entire test suite would 400.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "[::1]", "*.localhost", "testserver"],
)


@app.middleware("http")
async def enforce_local_access(request: Request, call_next):
    """Reject cross-origin browser access to the API.

    Browsers always attach an ``Origin`` header on cross-origin requests, so an
    Origin that is present and not localhost is a drive-by attempt and is
    refused. When the frontend token is present it must also match. Non-browser
    clients (no Origin, no token) pass – the loopback bind is their boundary.

    Both checks are deliberately "present and wrong ⇒ 403", never "absent ⇒
    403": tightening either into a hard requirement would lock out curl, editor
    plugins and the test suite, which is not the threat being defended against.
    Only ``/api/`` is guarded – the frontend routes serve public static assets
    and the token-bearing index.html itself, which must load with no headers set.
    """
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin is not None and not _LOCAL_ORIGIN_RE.match(origin):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request refused."})
        # getattr with a default: app.state.token only exists once lifespan has
        # run, and a request can arrive before that during startup.
        token_header = request.headers.get("x-studio-token")
        expected_token = getattr(app.state, "token", None)
        if token_header is not None and expected_token is not None and token_header != expected_token:
            return JSONResponse(status_code=403, content={"detail": "Invalid session token."})
    return await call_next(request)


# ─── API – status ────────────────────────────────────────────────────────────

@app.get("/api/status", summary="Check LaTeX tool availability")
async def get_status():
    """Return availability info for the LaTeX binaries and the default engine."""
    latex_status = check_latex_available()
    return {
        "latex_available": latex_status.get("pdflatex", {}).get("available", False),
        "tools": latex_status,
        "default_engine": DEFAULT_ENGINE,
        "shell_escape_enabled": ALLOW_SHELL_ESCAPE,
        "version": __version__,
    }


# ─── API – upload ────────────────────────────────────────────────────────────

@app.post("/api/upload", summary="Upload LaTeX project files")
async def upload_files(
    files: Annotated[List[UploadFile], File(description="LaTeX files or a .zip")],
):
    """Upload files (or a single ZIP). Returns a session_id and the file list.

    The workspace is created *before* the files are validated, so a rejected
    upload has to undo it – otherwise a user who uploads a bad ZIP would leave an
    empty folder behind on every attempt.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    session_id = create_session()
    logger.info(f"[{session_id}] Upload started ({len(files)} file(s)).")

    try:
        saved = await save_uploaded_files(session_id, files)
    except HTTPException:
        delete_session(session_id)  # do not leave a half-populated workspace behind
        raise

    session_dir = get_session_dir(session_id)
    touch_session(session_id)
    main_tex = detect_main_tex(session_dir)
    file_list = list_session_files(session_dir)
    logger.info(f"[{session_id}] Saved {len(saved)} file(s). Main: {main_tex}")

    return {
        "session_id": session_id,
        "files": file_list,
        "detected_main": main_tex,
        "message": f"Uploaded {len(saved)} file(s) successfully.",
    }


# ─── API – compile ───────────────────────────────────────────────────────────

@app.post("/api/compile", summary="Compile the LaTeX project")
async def compile_latex(
    session_id: Annotated[str, Form(description="Session ID from /api/upload")],
    main_file: Annotated[str | None, Form(description="Main .tex (auto-detected if omitted)")] = None,
    engine: Annotated[str, Form(description="pdflatex | xelatex | lualatex")] = DEFAULT_ENGINE,
    shell_escape: Annotated[bool, Form(description="Allow \\write18 for THIS compile (minted). Trusted documents only.")] = False,
):
    """Compile the project and return status, structured log, and the PDF URL.

    Takes form fields rather than JSON so the same endpoint could be driven from
    a plain HTML form; the response is JSON either way.

    Note that a compile that *fails* is still a 200 with ``success: false`` – a
    document with LaTeX errors is a normal outcome the UI must render, not an
    HTTP error. Only infrastructure problems raise.
    """
    if engine not in ENGINES:
        raise HTTPException(status_code=400, detail=f"Invalid engine '{engine}'. Choose from: {ENGINES}")

    session_dir = get_session_dir(session_id)
    touch_session(session_id)

    if not main_file:
        main_file = detect_main_tex(session_dir)
    if not main_file:
        raise HTTPException(status_code=400, detail="Could not detect a main .tex file. Specify 'main_file'.")

    # main_file must be one of the session's real .tex files (blocks traversal,
    # newlines and non-existent paths in one membership check).
    tex_files = {str(p.relative_to(session_dir)).replace("\\", "/") for p in session_dir.rglob("*.tex")}
    if main_file.replace("\\", "/") not in tex_files:
        raise HTTPException(status_code=400, detail="main_file must be one of the project's .tex files.")
    main_file = main_file.replace("\\", "/")

    # Refuse a second concurrent compile on the same session (the check and the
    # add happen without an await between them, so this is atomic on the loop).
    if session_id in _compiling:
        raise HTTPException(status_code=409, detail="A compile is already running for this session.")
    _compiling.add(session_id)

    # Shell-escape is enabled only when the config default says so OR the caller
    # explicitly opted in for this compile (the UI checkbox). Off by default.
    use_shell_escape = bool(ALLOW_SHELL_ESCAPE or shell_escape)
    logger.info(f"[{session_id}] Compiling '{main_file}' with {engine} "
                f"(shell-escape={use_shell_escape}).")
    if shell_escape and not ALLOW_SHELL_ESCAPE:
        logger.warning(f"[{session_id}] Shell-escape enabled for this compile by user request.")
    try:
        # compile_project() blocks on a subprocess for up to COMPILE_TIMEOUT
        # (10 minutes by default). Awaiting it directly would pin the single
        # event-loop thread for that whole time and freeze every other request,
        # so it is handed to a worker thread instead.
        result = await run_in_threadpool(
            compile_project, session_dir, main_file, engine, COMPILE_TIMEOUT, use_shell_escape
        )
    except Exception as exc:
        logger.error(f"[{session_id}] Unexpected compilation error: {exc}")
        raise HTTPException(status_code=500, detail=f"Internal compilation error: {exc}")
    finally:
        # In `finally` so a crash or a timeout cannot leave the session
        # permanently marked as compiling, which would 409 every later attempt.
        _compiling.discard(session_id)

    logger.info(f"[{session_id}] Compilation {'succeeded' if result.success else 'FAILED'} "
                f"in {result.duration_seconds:.1f}s. {result.summary}")

    # Remember exactly what this compile produced so /api/pdf and /api/log serve
    # that file, not the newest-by-mtime guess. The log is recorded even when the
    # compile failed – a failed compile is precisely when the user needs it.
    if result.log_path:
        _last_log[session_id] = result.log_path
    response = result.to_dict()
    response.update({"session_id": session_id, "main_file": main_file, "engine": engine,
                     "shell_escape": use_shell_escape})
    if result.success and result.pdf_path:
        _last_pdf[session_id] = result.pdf_path
        response["pdf_url"] = f"/api/pdf/{session_id}"
    else:
        # Forget the previous run's PDF and advertise no pdf_url: after a failed
        # compile the old document is stale, and the UI must not present it as
        # this compile's output.
        _last_pdf.pop(session_id, None)
        response["pdf_url"] = None
    return response


# ─── API – artifacts (PDF & log) ─────────────────────────────────────────────

@app.get("/api/pdf/{session_id}", summary="Download the compiled PDF")
async def get_pdf(session_id: str, download: bool = False):
    """Stream the PDF produced by the last successful compile of this session.

    ``?download=1`` switches the Content-Disposition from ``inline`` (the
    embedded viewer renders it in place) to ``attachment`` (the browser saves
    it); the bytes are identical either way.
    """
    session_dir = get_session_dir(session_id)  # validates format + existence
    touch_session(session_id)

    pdf_path = _last_pdf.get(session_id)
    if not pdf_path or not pdf_path.exists():
        # Fall back to disk so a still-open browser survives a server restart
        # (the in-memory map is empty after a restart, but the PDF is on disk).
        pdf_path = _find_artifact_on_disk(session_dir, ".pdf")
        if pdf_path:
            _last_pdf[session_id] = pdf_path
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="No compiled PDF. Compile the project first.")

    disposition = "attachment" if download else "inline"
    # The advertised name is always "output.pdf", never the session's real
    # filename – frontend/app.js sets the same literal on its download link, so
    # changing it here means changing it there too.
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        headers={"Content-Disposition": f"{disposition}; filename=output.pdf"},
    )


@app.get("/api/log/{session_id}", summary="Get the compilation log")
async def get_log(session_id: str, parsed: bool = False):
    """Return the log from the last compile (raw, or ?parsed=true structured).

    Both shapes are served from one endpoint because they are the same bytes:
    the UI shows the parsed errors/warnings, while "view raw log" needs the
    untouched text for anyone diagnosing a parser gap.
    """
    session_dir = get_session_dir(session_id)
    touch_session(session_id)

    log_path = _last_log.get(session_id)
    if not log_path or not log_path.exists():
        log_path = _find_artifact_on_disk(session_dir, ".log")  # survive a restart
        if log_path:
            _last_log[session_id] = log_path
    if not log_path or not log_path.exists():
        raise HTTPException(status_code=404, detail="No log file. Compile the project first.")

    # errors="replace": TeX logs are not reliably UTF-8 (engines emit bytes from
    # font names and package messages in their own encodings), and a log the user
    # cannot read is far worse than one with a few replacement characters.
    raw_log = log_path.read_text(encoding="utf-8", errors="replace")
    if parsed:
        return JSONResponse(content=parse_log(raw_log).to_dict())
    return JSONResponse(content={"raw": raw_log})


# ─── API – file browsing & editing ───────────────────────────────────────────

@app.get("/api/files/{session_id}", summary="List uploaded files")
async def list_files(session_id: str):
    """List the files in a session workspace.

    ``detected_main`` is re-computed on every listing rather than remembered from
    the upload, because the user may since have added or renamed a .tex file in
    the editor.
    """
    session_dir = get_session_dir(session_id)
    touch_session(session_id)
    return {"files": list_session_files(session_dir), "detected_main": detect_main_tex(session_dir)}


@app.get("/api/files/{session_id}/{filepath:path}", summary="Read a specific file")
async def read_file_endpoint(session_id: str, filepath: str):
    """Return a file's contents (images with their mime type, else text).

    Path safety is delegated to ``read_file_content``, which confines *filepath*
    to the session workspace.
    """
    import mimetypes

    session_dir = get_session_dir(session_id)
    touch_session(session_id)
    content = read_file_content(session_dir, filepath)
    mime_type, _ = mimetypes.guess_type(filepath)
    if mime_type and mime_type.startswith("image/"):
        return Response(content=content, media_type=mime_type)
    # Everything non-image is served as text/plain, never as its guessed type: a
    # project may contain .html or .svg, and serving those with their real mime
    # type would let an uploaded file run scripts on this app's own origin —
    # exactly the same-origin trust the token in index.html depends on.
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/api/files/{session_id}/{filepath:path}", summary="Update a specific file")
async def write_file_endpoint(session_id: str, filepath: str, request: Request):
    """Overwrite a file's contents (size-capped, extension-validated).

    The body is read in bounded chunks and rejected the moment it exceeds the
    limit — mirroring the streamed upload path, so a huge PUT cannot buffer
    unbounded memory (an oversized Content-Length is refused up front).
    """
    session_dir = get_session_dir(session_id)

    # Cheap pre-check: refuse an oversized body before reading a single byte.
    # It is only a hint (the header can lie or be absent, hence the streamed
    # check below), but it saves transferring a huge file just to reject it.
    declared_size = request.headers.get("content-length")
    if declared_size and declared_size.isdigit() and int(declared_size) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.")

    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        # Bail on the running total, not at the end: a lying Content-Length must
        # not be able to buffer an unbounded body in memory first.
        if total_bytes > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.")
        chunks.append(chunk)
    content = b"".join(chunks)

    # Re-check existence after reading the body so a concurrent cleanup cannot be
    # "resurrected" into a ghost session by the write.
    if not (UPLOAD_DIR / session_id).exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    write_file_content(session_dir, filepath, content)
    touch_session(session_id)
    return {"message": "File updated successfully."}


# ─── API – session lifecycle ─────────────────────────────────────────────────

@app.delete("/api/cleanup/{session_id}", summary="Delete the session workspace")
async def cleanup(session_id: str):
    """Remove a session workspace (refused while a compile is in flight).

    Deleting mid-compile would pull the files out from under a running LaTeX
    process, so an in-flight session is a 409 rather than a silent race.
    """
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    if session_id in _compiling:
        raise HTTPException(status_code=409, detail="Cannot delete a session while it is compiling.")
    delete_session(session_id)
    _last_pdf.pop(session_id, None)  # the artifacts are gone; do not keep dead paths
    _last_log.pop(session_id, None)
    logger.info(f"[{session_id}] Session cleaned up.")
    return {"message": f"Session '{session_id}' deleted successfully."}


# ─── Static files / frontend ─────────────────────────────────────────────────
# These routes are declared LAST on purpose: the catch-all "/{filename:path}"
# below matches literally any path, so every /api/ route must already be
# registered above it or it would be shadowed.

# Vendored assets (Monaco, fonts) and the app's own CSS/JS are served here.
# Serving the UI from this same app is what makes the frontend same-origin with
# the API, which is the premise the Origin check and the token both rest on.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend(request: Request):
    """Serve index.html with the per-instance token injected.

    The token is placed into the page the browser loads over the same origin, so
    only same-origin scripts can read it – a cross-origin attacker cannot.

    index.html is read from disk on every request rather than cached, because the
    substitution has to happen anyway and re-reading keeps edits to the UI live
    without a server restart.
    """
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(status_code=503, content={"detail": "Frontend not found. Run from the project root."})
    html = index_path.read_text(encoding="utf-8")
    html = html.replace("__STUDIO_TOKEN__", getattr(request.app.state, "token", ""))
    return HTMLResponse(content=html)


@app.get("/{filename:path}", include_in_schema=False)
async def serve_static(filename: str):
    """Serve a frontend asset, strictly confined to the frontend directory.

    The request string is never joined onto a directory. Each path component is
    checked against a strict allow-list of characters first, so ``..``, absolute
    paths, drive letters and NTFS stream names are rejected outright rather than
    being normalised away afterwards. The resolved result is then still required
    to sit inside FRONTEND_DIR - two independent guards, either of which alone
    would stop ``/%2e%2e/backend/config.py``.
    """
    frontend_root = FRONTEND_DIR.resolve()

    # Reject-then-build, rather than build-then-check.
    components = [part for part in re.split(r"[\\/]+", filename) if part]
    if not components or any(not _SAFE_PATH_COMPONENT.fullmatch(part) for part in components):
        raise HTTPException(status_code=404, detail="File not found.")

    target = frontend_root.joinpath(*components).resolve()
    # Belt and braces: even with only safe components, confirm the result did not
    # escape (e.g. via a symlink inside the frontend directory). relative_to() is
    # used instead of a string prefix test because "frontend-backup/" starts with
    # "frontend" but is a different directory.
    try:
        target.relative_to(frontend_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found.")
    if target.is_file():
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail="File not found.")
