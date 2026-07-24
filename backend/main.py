"""
main.py – FastAPI application entry point.

REST API endpoints:
  POST   /api/upload          – Upload project files, get session_id
  POST   /api/compile         – Compile the project, get log + PDF URL
  GET    /api/pdf/{session}   – Stream the compiled PDF
  GET    /api/log/{session}   – Get the full raw .log
  GET    /api/files/{session} – List files in the session workspace
  GET    /api/status          – Check LaTeX installation status
  DELETE /api/cleanup/{session} – Remove the session workspace
  GET    /                    – Serve the frontend index.html
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, List, Literal

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.compiler import compile_project, check_latex_available, EngineType
from backend.config import UPLOAD_DIR, FRONTEND_DIR, DEFAULT_ENGINE
from backend.file_manager import (
    create_session,
    delete_session,
    cleanup_stale_sessions,
    detect_main_tex,
    get_session_dir,
    list_session_files,
    save_uploaded_files,
    read_file_content,
    write_file_content,
)
from backend.log_parser import parse_log

# ─── Logging Setup ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("latex_app")


# ─── Lifespan (startup / shutdown) ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks before serving, and cleanup on shutdown."""
    # Ensure upload directory exists
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up stale sessions from previous runs
    deleted = cleanup_stale_sessions()
    if deleted:
        logger.info(f"Cleaned up {deleted} stale session(s) on startup.")

    # Check LaTeX availability and log status
    latex_status = check_latex_available()
    available = [k for k, v in latex_status.items() if v["available"]]
    missing = [k for k, v in latex_status.items() if not v["available"]]
    if available:
        logger.info(f"LaTeX tools available: {', '.join(available)}")
    if missing:
        logger.warning(f"LaTeX tools NOT found: {', '.join(missing)}")
    if not latex_status.get("pdflatex", {}).get("available"):
        logger.error(
            "pdflatex is NOT available! Please install MiKTeX or TeX Live. "
            "The application will start but compilation will fail."
        )

    yield  # App is running

    logger.info("Application shutting down.")


# ─── App Instance ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="LaTeX-to-PDF Compiler",
    description="Upload LaTeX project files and compile them to PDF in the browser.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow frontend (same origin or localhost during development) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.get("/api/status", summary="Check LaTeX tool availability")
async def get_status():
    """Returns availability info for all required LaTeX binaries."""
    latex_status = check_latex_available()
    pdflatex_ok = latex_status.get("pdflatex", {}).get("available", False)
    return {
        "latex_available": pdflatex_ok,
        "tools": latex_status,
        "default_engine": DEFAULT_ENGINE,
    }


@app.post("/api/upload", summary="Upload LaTeX project files")
async def upload_files(
    files: Annotated[List[UploadFile], File(description="LaTeX project files (.tex, .bib, images, .zip, etc.)")],
):
    """
    Upload one or more project files (or a single ZIP of the whole project).
    Returns a session_id used for subsequent compile/pdf/cleanup calls.
    """
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
    main_file: Annotated[str | None, Form(description="Main .tex file (auto-detected if omitted)")] = None,
    engine: Annotated[str, Form(description="LaTeX engine: pdflatex | xelatex | lualatex")] = DEFAULT_ENGINE,
):
    """
    Compile the uploaded LaTeX project.
    Returns compilation status, structured log (errors/warnings), and the PDF URL.
    """
    # Validate engine
    valid_engines = ("pdflatex", "xelatex", "lualatex")
    if engine not in valid_engines:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid engine '{engine}'. Choose from: {valid_engines}"
        )

    session_dir = get_session_dir(session_id)

    # Determine main .tex file
    if not main_file:
        main_file = detect_main_tex(session_dir)
    if not main_file:
        raise HTTPException(
            status_code=400,
            detail="Could not detect a main .tex file. Please specify 'main_file' in the request."
        )

    # Validate main_file path is inside session_dir (prevent traversal)
    try:
        (session_dir / main_file).resolve().relative_to(session_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid main_file path.")

    logger.info(f"[{session_id}] Compiling '{main_file}' with {engine}...")

    try:
        result = compile_project(
            workspace=session_dir,
            main_tex=main_file,
            engine=engine,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.error(f"[{session_id}] Unexpected compilation error: {e}")
        raise HTTPException(status_code=500, detail=f"Internal compilation error: {e}")

    logger.info(
        f"[{session_id}] Compilation {'succeeded' if result.success else 'FAILED'} "
        f"in {result.duration_seconds:.1f}s. {result.summary}"
    )

    response = result.to_dict()
    response["session_id"] = session_id
    response["main_file"] = main_file
    response["engine"] = engine

    if result.success:
        response["pdf_url"] = f"/api/pdf/{session_id}"
    else:
        response["pdf_url"] = None

    return response


@app.get("/api/pdf/{session_id}", summary="Download the compiled PDF")
async def get_pdf(session_id: str):
    """
    Stream the compiled PDF to the client.
    Sets appropriate Content-Disposition for both inline preview and download.
    """
    session_dir = get_session_dir(session_id)

    # Find any PDF in the session directory (prefer the one matching main.tex name)
    pdf_files = list(session_dir.rglob("*.pdf"))
    # Exclude any PDFs that were uploaded (only the compiled output)
    # Compiled PDFs will be in subdirs matching where the .tex file was
    tex_files = list(session_dir.rglob("*.tex"))
    compiled_pdfs = []
    for pdf in pdf_files:
        stem = pdf.stem
        matching_tex = [t for t in tex_files if t.stem == stem]
        if matching_tex:
            compiled_pdfs.append(pdf)

    if not compiled_pdfs:
        compiled_pdfs = pdf_files

    if not compiled_pdfs:
        raise HTTPException(
            status_code=404,
            detail="No compiled PDF found. Please compile the project first."
        )

    # Return the most recently modified PDF
    pdf_path = max(compiled_pdfs, key=lambda p: p.stat().st_mtime)

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename="output.pdf",
        headers={"Content-Disposition": "inline; filename=output.pdf"},
    )


@app.get("/api/log/{session_id}", summary="Get the raw compilation log")
async def get_log(session_id: str, parsed: bool = False):
    """
    Return the LaTeX compilation log.
    Set ?parsed=true for structured JSON (errors, warnings, etc.).
    """
    session_dir = get_session_dir(session_id)

    # Find the most recent .log file
    log_files = sorted(session_dir.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        raise HTTPException(status_code=404, detail="No log file found. Compile the project first.")

    log_file = log_files[0]
    raw = log_file.read_text(encoding="utf-8", errors="ignore")

    if parsed:
        return JSONResponse(content=parse_log(raw).to_dict())

    return JSONResponse(content={"raw": raw})


@app.get("/api/files/{session_id}", summary="List uploaded files")
async def list_files(session_id: str):
    """Return the list of files in the session workspace."""
    session_dir = get_session_dir(session_id)
    files = list_session_files(session_dir)
    main_tex = detect_main_tex(session_dir)
    return {"files": files, "detected_main": main_tex}


@app.get("/api/files/{session_id}/{filepath:path}", summary="Read a specific file")
async def read_file_endpoint(session_id: str, filepath: str):
    """Return the contents of a specific file in the workspace."""
    session_dir = get_session_dir(session_id)
    content = read_file_content(session_dir, filepath)
    
    # Guess mime type to display images properly
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filepath)
    if mime_type and mime_type.startswith("image/"):
        from fastapi import Response
        return Response(content=content, media_type=mime_type)
        
    # Return as raw text for code editor
    from fastapi import Response
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/api/files/{session_id}/{filepath:path}", summary="Update a specific file")
async def write_file_endpoint(
    session_id: str, 
    filepath: str, 
    request: Request
):
    """Overwrite the contents of a specific file."""
    session_dir = get_session_dir(session_id)
    content = await request.body()
    write_file_content(session_dir, filepath, content)
    return {"message": "File updated successfully."}


@app.delete("/api/cleanup/{session_id}", summary="Delete the session workspace")
async def cleanup(session_id: str):
    """Remove all files associated with a session."""
    import re
    # Validate session_id format before attempting deletion
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    delete_session(session_id)
    logger.info(f"[{session_id}] Session cleaned up.")
    return {"message": f"Session '{session_id}' deleted successfully."}


# ─── Static Files / Frontend ─────────────────────────────────────────────────

# Serve frontend static files
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    """Serve the main frontend HTML page."""
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend not found. Run from the project root."}
        )
    return FileResponse(str(index_path))


@app.get("/{filename:path}", include_in_schema=False)
async def serve_static(filename: str):
    """Serve any other frontend file (CSS, JS, images)."""
    file_path = FRONTEND_DIR / filename
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="File not found.")
