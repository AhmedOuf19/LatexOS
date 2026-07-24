"""
file_manager.py – File upload, session workspace management, and validation.

Responsibilities:
  - Create isolated UUID-based session directories under UPLOAD_DIR/
  - Validate uploaded file types and total sizes
  - Extract ZIP archives
  - Auto-detect the main .tex entry point
  - Clean up stale sessions
"""

import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import List, Tuple

from fastapi import UploadFile, HTTPException

from backend.config import (
    UPLOAD_DIR,
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_SIZE_BYTES,
    SESSION_TTL_SECONDS,
    MAX_UPLOAD_SIZE_MB,
)


# ─── Session Management ───────────────────────────────────────────────────────

def create_session() -> str:
    """Create a new session directory and return the session_id (UUID)."""
    session_id = str(uuid.uuid4())
    session_dir = UPLOAD_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_id


def get_session_dir(session_id: str) -> Path:
    """Return the workspace path for a session, validating it exists."""
    # Sanitize: session_id must be a plain UUID (no path traversal)
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    session_dir = UPLOAD_DIR / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session_dir


def delete_session(session_id: str) -> None:
    """Remove the entire session workspace directory."""
    try:
        session_dir = get_session_dir(session_id)
        shutil.rmtree(session_dir, ignore_errors=True)
    except HTTPException:
        pass  # Already deleted or invalid – that's fine


def cleanup_stale_sessions() -> int:
    """Delete session directories older than SESSION_TTL_SECONDS. Returns count deleted."""
    if not UPLOAD_DIR.exists():
        return 0
    now = time.time()
    deleted = 0
    for child in UPLOAD_DIR.iterdir():
        if child.is_dir():
            age = now - child.stat().st_mtime
            if age > SESSION_TTL_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
                deleted += 1
    return deleted


# ─── File Validation ──────────────────────────────────────────────────────────

def _validate_extension(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{suffix}' is not allowed. "
                   f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )


def _safe_filename(filename: str) -> str:
    """
    Sanitize a filename to prevent path traversal.
    Replaces any directory separators and strips leading dots/slashes.
    """
    # Normalize separators, then take only the final component
    safe = Path(filename).name
    # Remove characters that could be dangerous on Windows/Linux filesystems
    safe = re.sub(r'[<>:"|?*\x00-\x1f]', "_", safe)
    safe = safe.strip(". ")
    if not safe:
        safe = "unnamed_file"
    return safe


# ─── File Saving ──────────────────────────────────────────────────────────────

async def save_uploaded_files(
    session_id: str, files: List[UploadFile]
) -> List[str]:
    """
    Save a list of UploadFile objects into the session directory.
    Handles ZIP extraction automatically.
    Returns list of saved filenames (relative to session dir).
    """
    session_dir = get_session_dir(session_id)
    saved: List[str] = []
    total_size = 0

    for upload in files:
        filename = _safe_filename(upload.filename or "unnamed")
        _validate_extension(filename)

        # Read content into memory (respecting size limit)
        content = await upload.read()
        total_size += len(content)

        if total_size > MAX_UPLOAD_SIZE_BYTES:
            # Clean up and reject
            shutil.rmtree(session_dir, ignore_errors=True)
            raise HTTPException(
                status_code=413,
                detail=f"Total upload size exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
            )

        dest_path = session_dir / filename

        # Handle ZIP archives
        if filename.lower().endswith(".zip"):
            saved.extend(_extract_zip(content, session_dir))
        else:
            dest_path.write_bytes(content)
            saved.append(filename)

    return saved


def _extract_zip(content: bytes, session_dir: Path) -> List[str]:
    """
    Extract a ZIP archive into the session directory.
    Validates each extracted file against the whitelist.
    Guards against zip-slip path traversal attacks.
    """
    import io
    extracted: List[str] = []

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for member in zf.infolist():
            # Skip directories
            if member.filename.endswith("/"):
                continue

            # Zip-slip guard: resolve the final path and confirm it's inside session_dir
            member_path = (session_dir / member.filename).resolve()
            try:
                member_path.relative_to(session_dir.resolve())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Malicious ZIP entry detected: '{member.filename}'"
                )

            # Validate extension
            suffix = Path(member.filename).suffix.lower()
            if suffix not in ALLOWED_EXTENSIONS and suffix not in {"", ".tex"}:
                continue  # Skip disallowed files silently

            # Create subdirectory if needed
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(zf.read(member.filename))
            extracted.append(member.filename)

    return extracted


# ─── Main .tex Detection ──────────────────────────────────────────────────────

def detect_main_tex(session_dir: Path) -> str | None:
    """
    Intelligently detect the main .tex entry point in a project.

    Priority order:
    1. A file named exactly 'main.tex' in the root
    2. A single .tex file in the root (if only one exists)
    3. The .tex file that contains \\documentclass (most comprehensive search)
    4. The .tex file with \\begin{document} closest to root
    """
    tex_files = list(session_dir.rglob("*.tex"))

    if not tex_files:
        return None

    # Priority 1: main.tex in root
    root_main = session_dir / "main.tex"
    if root_main.exists():
        return "main.tex"

    # Priority 2: single .tex file at root level
    root_tex = [f for f in tex_files if f.parent == session_dir]
    if len(root_tex) == 1:
        return root_tex[0].name

    # Priority 3: search for \documentclass
    documentclass_files: List[Tuple[Path, int]] = []
    for tex in tex_files:
        try:
            text = tex.read_text(encoding="utf-8", errors="ignore")
            if r"\documentclass" in text:
                # Prefer files closer to root (shorter relative path)
                depth = len(tex.relative_to(session_dir).parts)
                documentclass_files.append((tex, depth))
        except OSError:
            continue

    if documentclass_files:
        # Sort by depth (ascending), then filename length (ascending)
        documentclass_files.sort(key=lambda x: (x[1], len(x[0].name)))
        best = documentclass_files[0][0]
        return str(best.relative_to(session_dir)).replace("\\", "/")

    # Priority 4: any .tex with \begin{document}
    for tex in tex_files:
        try:
            text = tex.read_text(encoding="utf-8", errors="ignore")
            if r"\begin{document}" in text:
                return str(tex.relative_to(session_dir)).replace("\\", "/")
        except OSError:
            continue

    # Last resort: first tex file found
    return str(tex_files[0].relative_to(session_dir)).replace("\\", "/")


def list_session_files(session_dir: Path) -> List[dict]:
    """
    Return a tree-like structure of files in the session directory.
    Each entry: {name, path, type, size}
    """
    result = []
    for item in sorted(session_dir.rglob("*")):
        if item.is_file():
            rel = str(item.relative_to(session_dir)).replace("\\", "/")
            result.append({
                "name": item.name,
                "path": rel,
                "ext": item.suffix.lower(),
                "size": item.stat().st_size,
            })
    return result


def _resolve_secure_path(session_dir: Path, filepath: str) -> Path:
    """Resolve and validate a path to ensure it is strictly within the session dir."""
    # Prevent absolute paths or root references in the user input
    clean_filepath = filepath.lstrip("/\\")
    target_path = (session_dir / clean_filepath).resolve()
    
    # Path traversal guard: must be inside session_dir
    try:
        target_path.relative_to(session_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")
        
    return target_path


def read_file_content(session_dir: Path, filepath: str) -> bytes:
    """Read file content securely from the session directory."""
    target_path = _resolve_secure_path(session_dir, filepath)
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if not target_path.is_file():
        raise HTTPException(status_code=400, detail="Target is not a file.")
        
    return target_path.read_bytes()


def write_file_content(session_dir: Path, filepath: str, content: bytes) -> None:
    """Write file content securely to the session directory."""
    target_path = _resolve_secure_path(session_dir, filepath)
    
    # Validate extension before saving
    _validate_extension(target_path.name)
    
    # Create parent directories if they don't exist
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
