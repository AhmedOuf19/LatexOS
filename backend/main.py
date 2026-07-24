"""
main.py – FastAPI application: routes, security middleware, and lifespan.

Security model (this app has NO authentication and is meant for local use)
--------------------------------------------------------------------------
* Binds to 127.0.0.1 by default (see backend/config.HOST).
* The frontend is served *same-origin* by this same app, so no cross-origin
  access is needed. A middleware therefore rejects any ``/api`` request whose
  ``Origin`` header is not a localhost origin – this blocks the "any website you
  visit drives your local API" (drive-by) attack.
* A per-instance random token is injected into ``index.html`` and echoed back by
  the frontend as ``X-Studio-Token``; when present it must match, giving a
  second layer of defence. Non-browser clients (tests, curl) send no Origin and
  no token and are allowed through the loopback bind.

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
    DEFAULT_ENGINE,
    ENGINES,
    FRONTEND_DIR,
    LOG_LEVEL,
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

# An Origin is trusted only if it is a localhost/loopback origin (any port).
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")

# Per-session state kept in memory (single-process, single-user design):
#   _last_pdf/_last_log – the exact artifact a compile produced, so /api/pdf and
#                         /api/log serve THAT file rather than guessing by mtime.
#   _compiling          – sessions with a compile in flight, to reject overlap.
_last_pdf: Dict[str, Path] = {}
_last_log: Dict[str, Path] = {}
_compiling: set[str] = set()


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: prepare dirs, mint the instance token, log LaTeX status, and
    start the background session cleaner. Shutdown: stop the cleaner."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # A fresh, unguessable token per server start.
    app.state.token = secrets.token_urlsafe(32)

    deleted = cleanup_stale_sessions()
    if deleted:
        logger.info(f"Cleaned up {deleted} stale session(s) on startup.")

    latex_status = check_latex_available()
    available = [k for k, v in latex_status.items() if v["available"]]
    missing = [k for k, v in latex_status.items() if not v["available"]]
    if available:
        logger.info(f"LaTeX tools available: {', '.join(available)}")
    if missing:
        logger.warning(f"LaTeX tools NOT found: {', '.join(missing)}")
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
    """Sweep stale sessions on an interval until cancelled."""
    while True:
        await asyncio.sleep(SESSION_GC_INTERVAL_SECONDS)
        try:
            removed = await run_in_threadpool(cleanup_stale_sessions)
            if removed:
                logger.info(f"Background cleanup removed {removed} stale session(s).")
        except Exception as exc:  # never let the loop die
            logger.warning(f"Session cleanup error: {exc}")


# ─── App Instance ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="LaTeX Studio",
    description="Upload LaTeX project files and compile them to PDF in the browser.",
    version=__version__,
    lifespan=lifespan,
)

# Blunt DNS-rebinding against the loopback service.
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
    """
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin is not None and not _LOCAL_ORIGIN_RE.match(origin):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request refused."})
        token_hdr = request.headers.get("x-studio-token")
        expected = getattr(app.state, "token", None)
        if token_hdr is not None and expected is not None and token_hdr != expected:
            return JSONResponse(status_code=403, content={"detail": "Invalid session token."})
    return await call_next(request)


# ─── API Routes ──────────────────────────────────────────────────────────────

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


@app.post("/api/upload", summary="Upload LaTeX project files")
async def upload_files(
    files: Annotated[List[UploadFile], File(description="LaTeX files or a .zip")],
):
    """Upload files (or a single ZIP). Returns a session_id and the file list."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    session_id = create_session()
    logger.info(f"[{session_id}] Upload started ({len(files)} file(s)).")

    try:
        saved = await save_uploaded_files(session_id, files)
    except HTTPException:
        delete_session(session_id)
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


@app.post("/api/compile", summary="Compile the LaTeX project")
async def compile_latex(
    session_id: Annotated[str, Form(description="Session ID from /api/upload")],
    main_file: Annotated[str | None, Form(description="Main .tex (auto-detected if omitted)")] = None,
    engine: Annotated[str, Form(description="pdflatex | xelatex | lualatex")] = DEFAULT_ENGINE,
):
    """Compile the project and return status, structured log, and the PDF URL."""
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

    logger.info(f"[{session_id}] Compiling '{main_file}' with {engine} (shell-escape={ALLOW_SHELL_ESCAPE}).")
    try:
        # Run the blocking compile OFF the event loop so other requests are not stalled.
        result = await run_in_threadpool(compile_project, session_dir, main_file, engine)
    except Exception as e:
        logger.error(f"[{session_id}] Unexpected compilation error: {e}")
        raise HTTPException(status_code=500, detail=f"Internal compilation error: {e}")
    finally:
        _compiling.discard(session_id)

    logger.info(f"[{session_id}] Compilation {'succeeded' if result.success else 'FAILED'} "
                f"in {result.duration_seconds:.1f}s. {result.summary}")

    # Remember exactly what this compile produced so /api/pdf and /api/log serve
    # that file, not the newest-by-mtime guess.
    if result.log_path:
        _last_log[session_id] = result.log_path
    response = result.to_dict()
    response.update({"session_id": session_id, "main_file": main_file, "engine": engine})
    if result.success and result.pdf_path:
        _last_pdf[session_id] = result.pdf_path
        response["pdf_url"] = f"/api/pdf/{session_id}"
    else:
        _last_pdf.pop(session_id, None)
        response["pdf_url"] = None
    return response


@app.get("/api/pdf/{session_id}", summary="Download the compiled PDF")
async def get_pdf(session_id: str, download: bool = False):
    """Stream the PDF produced by the last successful compile of this session."""
    get_session_dir(session_id)  # validates format + existence (raises otherwise)
    touch_session(session_id)

    pdf_path = _last_pdf.get(session_id)
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="No compiled PDF. Compile the project first.")

    disposition = "attachment" if download else "inline"
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        headers={"Content-Disposition": f"{disposition}; filename=output.pdf"},
    )


@app.get("/api/log/{session_id}", summary="Get the compilation log")
async def get_log(session_id: str, parsed: bool = False):
    """Return the log from the last compile (raw, or ?parsed=true structured)."""
    get_session_dir(session_id)
    touch_session(session_id)

    log_path = _last_log.get(session_id)
    if not log_path or not log_path.exists():
        raise HTTPException(status_code=404, detail="No log file. Compile the project first.")

    raw = log_path.read_text(encoding="utf-8", errors="replace")
    if parsed:
        return JSONResponse(content=parse_log(raw).to_dict())
    return JSONResponse(content={"raw": raw})


@app.get("/api/files/{session_id}", summary="List uploaded files")
async def list_files(session_id: str):
    """List the files in a session workspace."""
    session_dir = get_session_dir(session_id)
    touch_session(session_id)
    return {"files": list_session_files(session_dir), "detected_main": detect_main_tex(session_dir)}


@app.get("/api/files/{session_id}/{filepath:path}", summary="Read a specific file")
async def read_file_endpoint(session_id: str, filepath: str):
    """Return a file's contents (images with their mime type, else text)."""
    import mimetypes

    session_dir = get_session_dir(session_id)
    touch_session(session_id)
    content = read_file_content(session_dir, filepath)
    mime_type, _ = mimetypes.guess_type(filepath)
    if mime_type and mime_type.startswith("image/"):
        return Response(content=content, media_type=mime_type)
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/api/files/{session_id}/{filepath:path}", summary="Update a specific file")
async def write_file_endpoint(session_id: str, filepath: str, request: Request):
    """Overwrite a file's contents (size-capped, extension-validated)."""
    session_dir = get_session_dir(session_id)
    content = await request.body()
    # Re-check existence after reading the body so a concurrent cleanup cannot be
    # "resurrected" into a ghost session by the write.
    if not (UPLOAD_DIR / session_id).exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    write_file_content(session_dir, filepath, content)
    touch_session(session_id)
    return {"message": "File updated successfully."}


@app.delete("/api/cleanup/{session_id}", summary="Delete the session workspace")
async def cleanup(session_id: str):
    """Remove a session workspace (refused while a compile is in flight)."""
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    if session_id in _compiling:
        raise HTTPException(status_code=409, detail="Cannot delete a session while it is compiling.")
    delete_session(session_id)
    _last_pdf.pop(session_id, None)
    _last_log.pop(session_id, None)
    logger.info(f"[{session_id}] Session cleaned up.")
    return {"message": f"Session '{session_id}' deleted successfully."}


# ─── Static Files / Frontend ─────────────────────────────────────────────────

# Vendored assets (Monaco, fonts) and the app's own CSS/JS are served here.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend(request: Request):
    """Serve index.html with the per-instance token injected.

    The token is placed into the page the browser loads over the same origin, so
    only same-origin scripts can read it – a cross-origin attacker cannot.
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

    Unlike a naive join, the resolved path must stay inside FRONTEND_DIR, so
    ``/%2e%2e/backend/config.py`` and similar traversals return 404.
    """
    frontend_root = FRONTEND_DIR.resolve()
    target = (FRONTEND_DIR / filename).resolve()
    try:
        target.relative_to(frontend_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found.")
    if target.is_file():
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail="File not found.")
