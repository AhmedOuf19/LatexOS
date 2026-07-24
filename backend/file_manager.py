"""
file_manager.py – Uploads, session workspaces, and path safety.

Responsibilities
----------------
* Create isolated UUID-named session directories under ``uploads/``.
* Validate uploaded file types and enforce size limits *while streaming*
  (so an oversized file is rejected before it is fully buffered in memory).
* Extract ZIP archives safely: guard against zip-slip, zip-bombs, disallowed
  file types, and dangerous names (dotfiles such as ``.latexmkrc``).
* Auto-detect the main ``.tex`` entry point.
* Resolve editor file paths strictly inside the session (no traversal, no
  Windows alternate-data-streams, no reserved device names).
"""

from __future__ import annotations

import io
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import List, Tuple, Union

from fastapi import HTTPException, UploadFile

from backend.config import (
    ALLOWED_EXTENSIONS,
    MAX_EXTRACTED_SIZE_BYTES,
    MAX_UPLOAD_SIZE_BYTES,
    MAX_UPLOAD_SIZE_MB,
    MAX_ZIP_MEMBERS,
    SESSION_TTL_SECONDS,
    UPLOAD_DIR,
)

# Canonical UUID-v4 layout (8-4-4-4-12, lowercase hex). Anchored so nothing but
# a real session id can address a workspace.
SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Windows reserved device names – never allowed as a file component.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Bytes read per chunk when streaming an upload to disk.
_CHUNK = 1024 * 1024  # 1 MiB


# ─── Session Management ───────────────────────────────────────────────────────

def create_session() -> str:
    """Create a new session directory and return its id (a UUID-v4 string)."""
    session_id = str(uuid.uuid4())
    (UPLOAD_DIR / session_id).mkdir(parents=True, exist_ok=True)
    return session_id


def is_valid_session_id(session_id: str) -> bool:
    """True if ``session_id`` is a canonical UUID-v4 string."""
    return bool(SESSION_ID_RE.fullmatch(session_id))


def get_session_dir(session_id: str) -> Path:
    """Return the workspace for ``session_id`` (validating format + existence)."""
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    session_dir = UPLOAD_DIR / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session_dir


def touch_session(session_id: str) -> None:
    """Refresh a session's last-access time so it is not reaped while in use."""
    session_dir = UPLOAD_DIR / session_id
    if session_dir.exists():
        try:
            (session_dir / ".last_access").write_text(str(time.time()))
        except OSError:
            pass


def delete_session(session_id: str) -> None:
    """Remove a session workspace (no-op if it is already gone/invalid)."""
    if not is_valid_session_id(session_id):
        return
    shutil.rmtree(UPLOAD_DIR / session_id, ignore_errors=True)


def cleanup_stale_sessions() -> int:
    """Delete sessions untouched for longer than ``SESSION_TTL_SECONDS``.

    Last activity is read from the ``.last_access`` marker written on every
    request; if that is missing we fall back to the directory mtime. Returns the
    number of sessions removed.
    """
    if not UPLOAD_DIR.exists():
        return 0
    now = time.time()
    deleted = 0
    for child in UPLOAD_DIR.iterdir():
        if not child.is_dir():
            continue
        marker = child / ".last_access"
        try:
            last = float(marker.read_text()) if marker.exists() else child.stat().st_mtime
        except (OSError, ValueError):
            last = child.stat().st_mtime
        if now - last > SESSION_TTL_SECONDS:
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
    return deleted


# ─── File Validation ──────────────────────────────────────────────────────────

def _validate_extension(filename: str) -> None:
    """Reject any filename whose extension is not whitelisted."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{suffix or '(none)'}' is not allowed. "
                   f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )


def _safe_filename(filename: str) -> str:
    """Reduce a filename to a single safe path component.

    Strips any directory part, replaces characters that are dangerous on
    Windows/POSIX, drops leading dots (so dotfiles like ``.latexmkrc`` cannot be
    created), and rejects reserved device names.
    """
    safe = Path(filename).name
    safe = re.sub(r'[<>:"|?*\x00-\x1f]', "_", safe)
    safe = safe.strip(". ")           # no leading/trailing dots or spaces
    if not safe:
        safe = "unnamed_file"
    if safe.split(".")[0].lower() in _RESERVED_NAMES:
        safe = "_" + safe
    return safe


# ─── File Saving (streamed) ───────────────────────────────────────────────────

async def save_uploaded_files(session_id: str, files: List[UploadFile]) -> List[str]:
    """Stream uploaded files into the session directory.

    Each file is written in bounded chunks with a running size total, so the
    cumulative upload limit is enforced *before* memory is exhausted. ZIP
    archives are streamed to a temp file and then extracted safely.
    """
    session_dir = get_session_dir(session_id)
    saved: List[str] = []
    total_size = 0

    for upload in files:
        filename = _safe_filename(upload.filename or "unnamed")
        _validate_extension(filename)

        if filename.lower().endswith(".zip"):
            # Stream the archive to a temp file (bounded), then extract.
            tmp_zip = session_dir / f".upload-{uuid.uuid4().hex}.zip"
            total_size = await _stream_to_file(upload, tmp_zip, total_size, session_dir)
            try:
                saved.extend(_extract_zip(tmp_zip, session_dir))
            finally:
                tmp_zip.unlink(missing_ok=True)
        else:
            dest = session_dir / filename
            total_size = await _stream_to_file(upload, dest, total_size, session_dir)
            saved.append(filename)

    return saved


async def _stream_to_file(
    upload: UploadFile, dest: Path, total_so_far: int, session_dir: Path
) -> int:
    """Write an upload to ``dest`` in chunks, enforcing the cumulative limit.

    Returns the new running total. Raises 413 and cleans up the session if the
    limit is exceeded, without ever holding the whole file in memory.
    """
    total = total_so_far
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_SIZE_BYTES:
                out.close()
                shutil.rmtree(session_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Total upload size exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
                )
            out.write(chunk)
    return total


def _extract_zip(source: Union[bytes, str, Path], session_dir: Path) -> List[str]:
    """Safely extract a ZIP into the session directory.

    ``source`` may be raw bytes (used by tests) or a path to a .zip file. Guards
    applied to every member:
      * **zip-slip** – the resolved path must stay inside the session.
      * **type whitelist** – only allowed extensions are extracted (no more
        extensionless-file loophole that let ``.latexmkrc`` through).
      * **name sanitising** – each component is run through ``_safe_filename``.
      * **zip-bomb** – member count, per-member size and cumulative extracted
        size are all capped.
    """
    zf_source = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    extracted: List[str] = []
    total_out = 0
    session_root = session_dir.resolve()

    with zipfile.ZipFile(zf_source) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise HTTPException(
                status_code=400,
                detail=f"ZIP has too many entries (>{MAX_ZIP_MEMBERS}).",
            )

        for member in members:
            if member.is_dir():
                continue

            # Rebuild the relative path from sanitised components, preserving the
            # folder structure but neutralising traversal and dangerous names.
            raw_parts = [p for p in re.split(r"[\\/]+", member.filename) if p not in ("", ".", "..")]
            if not raw_parts:
                continue
            safe_parts = [_safe_filename(p) for p in raw_parts]
            target = (session_dir / Path(*safe_parts)).resolve()

            # zip-slip guard: must remain inside the session.
            try:
                target.relative_to(session_root)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Malicious ZIP entry detected: '{member.filename}'",
                )

            # Only extract whitelisted types – silently skip the rest.
            if Path(target.name).suffix.lower() not in ALLOWED_EXTENSIONS:
                continue

            # zip-bomb guards.
            total_out += member.file_size
            if member.file_size > MAX_EXTRACTED_SIZE_BYTES or total_out > MAX_EXTRACTED_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="ZIP contents exceed the allowed extracted size.",
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            # Stream the member so a single huge entry is not read into RAM.
            written = 0
            with zf.open(member) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_EXTRACTED_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="ZIP contents exceed the allowed extracted size.",
                        )
                    dst.write(chunk)
            extracted.append("/".join(safe_parts))

    return extracted


# ─── Main .tex Detection ──────────────────────────────────────────────────────

def detect_main_tex(session_dir: Path) -> str | None:
    """Detect the main ``.tex`` entry point.

    Priority: an exact ``main.tex`` in the root → the only root-level .tex →
    the .tex containing ``\\documentclass`` closest to the root →
    any .tex with ``\\begin{document}`` → the first .tex found.
    """
    tex_files = list(session_dir.rglob("*.tex"))
    if not tex_files:
        return None

    root_main = session_dir / "main.tex"
    if root_main.exists():
        return "main.tex"

    root_tex = [f for f in tex_files if f.parent == session_dir]
    if len(root_tex) == 1:
        return root_tex[0].name

    documentclass_files: List[Tuple[Path, int]] = []
    for tex in tex_files:
        try:
            text = tex.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if r"\documentclass" in text:
            depth = len(tex.relative_to(session_dir).parts)
            documentclass_files.append((tex, depth))

    if documentclass_files:
        documentclass_files.sort(key=lambda x: (x[1], len(x[0].name)))
        return _rel(documentclass_files[0][0], session_dir)

    for tex in tex_files:
        try:
            if r"\begin{document}" in tex.read_text(encoding="utf-8", errors="ignore"):
                return _rel(tex, session_dir)
        except OSError:
            continue

    return _rel(tex_files[0], session_dir)


def _rel(path: Path, root: Path) -> str:
    """Workspace-relative path with forward slashes (stable across OSes)."""
    return str(path.relative_to(root)).replace("\\", "/")


def list_session_files(session_dir: Path) -> List[dict]:
    """List the files in a session as ``{name, path, ext, size}`` entries.

    Internal bookkeeping files (the ``.last_access`` marker, temp uploads) are
    hidden from the user.
    """
    result = []
    for item in sorted(session_dir.rglob("*")):
        if not item.is_file():
            continue
        name = item.name
        if name == ".last_access" or name.startswith(".upload-"):
            continue
        result.append({
            "name": name,
            "path": _rel(item, session_dir),
            "ext": item.suffix.lower(),
            "size": item.stat().st_size,
        })
    return result


# ─── Secure file access for the editor endpoints ─────────────────────────────

def _resolve_secure_path(session_dir: Path, filepath: str) -> Path:
    """Resolve an editor path and guarantee it stays inside the session.

    Rejects traversal (``..``), Windows alternate-data-streams / drive-qualified
    paths (a ``:`` in any component) and absolute paths.
    """
    clean = filepath.lstrip("/\\")
    if ":" in clean:  # blocks C:\…, file.tex:$DATA, etc.
        raise HTTPException(status_code=400, detail="Invalid path.")

    target = (session_dir / clean).resolve()
    try:
        target.relative_to(session_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")
    return target


def read_file_content(session_dir: Path, filepath: str) -> bytes:
    """Read a file securely from the session workspace."""
    target = _resolve_secure_path(session_dir, filepath)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Target is not a file.")
    return target.read_bytes()


def write_file_content(session_dir: Path, filepath: str, content: bytes) -> None:
    """Write a file securely into the session workspace.

    Validates the extension and enforces the per-file size limit so a huge
    editor save cannot exhaust memory or disk.
    """
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
        )
    target = _resolve_secure_path(session_dir, filepath)
    _validate_extension(target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
