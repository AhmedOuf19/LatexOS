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

Threat model (why this file is so defensive)
--------------------------------------------
LaTeX Studio runs locally for one user, but the *content* it handles is not
trusted: people routinely download a journal template or a colleague's project
ZIP and drop it in. Two facts drive almost every decision below.

1. **The compiler is an interpreter.** ``latexmk`` sources a ``latexmkrc`` file
   found next to the document and runs the Perl inside it, and TeX itself can
   read and write files. Anything that lands in a workspace is a candidate for
   execution, so what may exist there is decided by a *whitelist* of extensions
   (``ALLOWED_EXTENSIONS``), never by a blacklist of bad ones.
2. **Everything inside an archive is attacker-controlled.** Member names, sizes
   and CRCs in a ZIP are just bytes someone else wrote. No value read from an
   archive header is treated as a fact; each one is either re-derived or
   re-checked against what actually happens on disk.

The recurring pattern is therefore **rebuild, don't validate**: instead of
inspecting a hostile name and deciding whether it looks safe, we throw it away
and construct a new name from sanitised components. Validation has to anticipate
every encoding trick that exists; reconstruction does not have to anticipate
anything.
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

# ─── Constants ────────────────────────────────────────────────────────────────

# Canonical UUID layout (8-4-4-4-12, lowercase hex). Always used with
# fullmatch(), so an id can never smuggle in a separator, a "..", or a drive
# letter — a validated session id is by construction a harmless single
# directory name. Only the *shape* is checked, not the version/variant nibbles:
# the goal is confinement to a name this app could have generated, not proof of
# where the id came from.
SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Windows reserved device names – never allowed as a file component (a name that
# hits this set is prefixed with "_"). Opening "con" or "lpt1" on Windows talks
# to a device instead of a file, which either fails bizarrely or hangs the
# compiler waiting on it. The rule applies whatever the extension
# ("aux.tex" is still the AUX device), which is why the check below compares the
# stem rather than the whole name. Enforced on every OS so a project behaves
# identically wherever it is unpacked.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Bytes read per chunk when streaming an upload to disk. Fixed size means memory
# use stays constant no matter how large the upload is, while being big enough
# that a 500 MB file costs a few hundred reads rather than millions.
_CHUNK = 1024 * 1024  # 1 MiB


# ─── Session Management ───────────────────────────────────────────────────────

def create_session() -> str:
    """Create a new session directory and return its id (a UUID-v4 string)."""
    session_id = str(uuid.uuid4())
    (UPLOAD_DIR / session_id).mkdir(parents=True, exist_ok=True)
    return session_id


def is_valid_session_id(session_id: str) -> bool:
    """Return True if ``session_id`` is a canonical UUID-v4 string.

    This is the gate every session-scoped route passes through, so it is a
    security check as much as a format check: anything that is not exactly this
    shape can never be turned into a filesystem path.
    """
    return bool(SESSION_ID_RE.fullmatch(session_id))


def get_session_dir(session_id: str) -> Path:
    """Return the workspace for ``session_id`` (validating format + existence).

    Distinguishes 400 (the id is malformed, i.e. the caller is wrong or hostile)
    from 404 (the id is well-formed but the session expired or was deleted), so
    the frontend can offer "start a new project" only in the second case.
    """
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    session_dir = UPLOAD_DIR / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session_dir


def touch_session(session_id: str) -> None:
    """Refresh a session's last-access time so it is not reaped while in use.

    Called on every session-scoped request. Failures are swallowed deliberately:
    a marker that could not be written must never turn a working request into an
    error, and the worst consequence is that an idle-looking session is cleaned
    up a little early.
    """
    session_dir = UPLOAD_DIR / session_id
    if session_dir.exists():
        try:
            (session_dir / ".last_access").write_text(str(time.time()))
        except OSError:
            pass


def delete_session(session_id: str) -> None:
    """Remove a session workspace (no-op if it is already gone/invalid).

    Silent rather than raising because the callers are best-effort cleanup
    paths — the browser's tab-close request and the rollback after a failed
    upload — where there is nobody left to report an error to.
    """
    if not is_valid_session_id(session_id):
        return
    shutil.rmtree(UPLOAD_DIR / session_id, ignore_errors=True)


def cleanup_stale_sessions() -> int:
    """Delete sessions untouched for longer than ``SESSION_TTL_SECONDS``.

    Last activity is read from the ``.last_access`` marker written on every
    request; if that is missing we fall back to the directory mtime. Returns the
    number of sessions removed.

    The marker exists because directory mtime is the wrong clock: it only
    changes when entries are added or removed, so a user who spends an hour
    editing and re-saving existing files would look completely idle and have
    their project deleted underneath them.
    """
    if not UPLOAD_DIR.exists():
        return 0
    now = time.time()
    deleted_count = 0
    for session_dir in UPLOAD_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        access_marker = session_dir / ".last_access"
        try:
            last_access = (
                float(access_marker.read_text())
                if access_marker.exists()
                else session_dir.stat().st_mtime
            )
        except (OSError, ValueError):
            # A truncated or half-written marker must not stop the sweep; fall
            # back to mtime rather than leaving the session alive forever.
            last_access = session_dir.stat().st_mtime
        if now - last_access > SESSION_TTL_SECONDS:
            # ignore_errors: on Windows a PDF still open in a viewer holds a
            # lock. Skipping that one file is better than aborting the sweep and
            # leaking every session behind it; the next sweep retries.
            shutil.rmtree(session_dir, ignore_errors=True)
            deleted_count += 1
    return deleted_count


# ─── File Validation ──────────────────────────────────────────────────────────

def _validate_extension(filename: str) -> None:
    """Reject any filename whose extension is not whitelisted.

    A whitelist, not a blacklist: a LaTeX project needs a small, known set of
    file types, and anything outside it is more likely to be an attack than a
    legitimate asset. Note that ``Path("latexmkrc").suffix`` is ``""`` and the
    empty string is deliberately absent from ``ALLOWED_EXTENSIONS``, so
    extensionless files fall out of this check as rejected — hence the
    ``(none)`` wording in the error, which would otherwise read as a blank.
    """
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

    Never raises: a hostile name is rewritten into a harmless one rather than
    refused, because a single odd filename inside an otherwise fine project
    should not fail the whole upload. Refusal is the job of
    ``_validate_extension``, which runs on the *result* of this function.
    """
    # Split on BOTH separators explicitly rather than using Path(...).name: on
    # POSIX, pathlib does not treat "\" as a separator, so a Windows-style name
    # like "..\..\evil.tex" arrives as one component and would survive
    # sanitising completely intact. (Regression: this exact case shipped once
    # and only failed on Linux CI.)
    safe_name = re.split(r"[\\/]+", filename)[-1]

    # Characters Windows forbids, plus C0 control characters (which can hide the
    # real extension in the file tree and land raw in the DOM). ':' is the
    # dangerous one: "figure.png:payload" is an NTFS alternate data stream — a
    # second, invisible file riding along on a legitimate-looking name.
    safe_name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", safe_name)

    # Leading dot: the whole .latexmkrc attack (latexmk executes the Perl in a
    # config file found beside the document). Trailing dot/space: Windows
    # silently trims them, so "main.tex " and "main.tex" are the same file on
    # disk — stripping here keeps the name we report identical to the name that
    # actually exists.
    safe_name = safe_name.strip(". ")

    if not safe_name:
        safe_name = "unnamed_file"
    # Compare the stem, not the full name: reserved names stay reserved with an
    # extension attached, so "aux.tex" is still the AUX device.
    if safe_name.split(".")[0].lower() in _RESERVED_NAMES:
        safe_name = "_" + safe_name
    return safe_name


# ─── File Saving (streamed) ───────────────────────────────────────────────────

async def save_uploaded_files(session_id: str, files: List[UploadFile]) -> List[str]:
    """Stream uploaded files into the session directory.

    Each file is written in bounded chunks with a running size total, so the
    cumulative upload limit is enforced *before* memory is exhausted. ZIP
    archives are streamed to a temp file and then extracted safely.

    The limit is cumulative across the whole request, not per file: a hundred
    files just under the cap must not add up to a hundred times the cap.
    """
    session_dir = get_session_dir(session_id)
    saved: List[str] = []
    total_size = 0

    for upload in files:
        # Sanitise BEFORE validating: the extension check must judge the name
        # that will actually exist on disk, not the one the browser sent. The
        # two really do differ — pathlib sees no suffix at all on "chapter.tex."
        # although it is written as "chapter.tex" — and the on-disk name is what
        # latexmk will later act on.
        filename = _safe_filename(upload.filename or "unnamed")
        _validate_extension(filename)

        if filename.lower().endswith(".zip"):
            # To disk, not to memory: zipfile needs random access to the central
            # directory, so the archive cannot be processed as a pure stream.
            # The name is uuid-suffixed so concurrent uploads cannot collide, and
            # ".upload-" prefixed so list_session_files() never surfaces it as a
            # project file — including while it is still being streamed.
            tmp_zip = session_dir / f".upload-{uuid.uuid4().hex}.zip"
            total_size = await _stream_to_file(upload, tmp_zip, total_size, session_dir)
            try:
                saved.extend(_extract_zip(tmp_zip, session_dir))
            finally:
                # The archive itself is never part of the project; remove it even
                # when extraction raised, or a rejected zip-bomb would stay on
                # disk as the very thing the size cap was defending against.
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

    The size is checked as the bytes arrive rather than from the Content-Length
    header, because a header is a claim and the chunk loop is a measurement.
    """
    running_total = total_so_far
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out_file:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            running_total += len(chunk)
            if running_total > MAX_UPLOAD_SIZE_BYTES:
                # Close before deleting: Windows refuses to remove a file that
                # still has an open handle, so skipping this would leave the
                # oversized partial file behind. (Closing twice is harmless —
                # the enclosing `with` is idempotent.)
                out_file.close()
                # Drop the entire workspace, not just this file: a half-uploaded
                # project is useless to the user, and it stops a rejected upload
                # from being reused as free storage.
                shutil.rmtree(session_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Total upload size exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
                )
            out_file.write(chunk)
    return running_total


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

    ``zipfile.extractall`` is deliberately not used. It sanitises member names,
    but it has no notion of a type whitelist or a size cap — the two guards that
    matter most here — and it offers no hook to inspect a member before its
    bytes land on disk.

    A member that trips a size cap raises mid-extraction, leaving earlier
    members on disk. That is safe because the only caller (``/api/upload``)
    deletes the whole session when this raises.
    """
    zf_source = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    extracted: List[str] = []
    declared_total_bytes = 0
    session_root = session_dir.resolve()

    with zipfile.ZipFile(zf_source) as archive:
        members = archive.infolist()
        # Capped separately from total size because the two attacks are
        # different: a million empty members costs almost no bytes, but each one
        # still buys a resolve(), an mkdir() and a file handle.
        if len(members) > MAX_ZIP_MEMBERS:
            raise HTTPException(
                status_code=400,
                detail=f"ZIP has too many entries (>{MAX_ZIP_MEMBERS}).",
            )

        for member in members:
            # Directory entries carry no data; the parents we actually need are
            # created below from the sanitised file paths.
            if member.is_dir():
                continue

            # Rebuild the relative path from sanitised components, preserving the
            # folder structure but neutralising traversal and dangerous names.
            # Discarding "" / "." / ".." here means a traversal payload is gone
            # before a Path object is ever constructed from it — the confinement
            # check below is then a backstop, not the only line of defence.
            raw_parts = [p for p in re.split(r"[\\/]+", member.filename) if p not in ("", ".", "..")]
            if not raw_parts:
                continue
            safe_parts = [_safe_filename(p) for p in raw_parts]
            target = (session_dir / Path(*safe_parts)).resolve()

            # zip-slip backstop: the target must still be inside the session.
            # Sanitising above should make this unreachable, which is exactly why
            # it stays — it is the one invariant that has to hold even if
            # _safe_filename is later weakened. resolve() first, so the check is
            # against the real destination (".." collapsed, symlinks followed)
            # rather than against the text of the name.
            try:
                target.relative_to(session_root)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Malicious ZIP entry detected: '{member.filename}'",
                )

            # Only extract whitelisted types – silently skip the rest.
            # This is the RCE guard, not a tidiness rule: an empty suffix is not
            # whitelisted, so "latexmkrc" (which latexmk sources and executes as
            # Perl from the working directory) can never be written. Skipping is
            # silent because real project ZIPs are full of .gitignore, LICENSE
            # and editor droppings, and failing the upload over them would help
            # nobody.
            if Path(target.name).suffix.lower() not in ALLOWED_EXTENSIONS:
                continue

            # Zip-bomb guard 1 of 2 – the size the archive *claims*. Cheap, and
            # it rejects the classic 42 KB → petabyte archive before a single
            # byte is decompressed.
            declared_total_bytes += member.file_size
            if member.file_size > MAX_EXTRACTED_SIZE_BYTES or declared_total_bytes > MAX_EXTRACTED_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="ZIP contents exceed the allowed extracted size.",
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            # Stream the member so a single huge entry is not read into RAM.
            written_bytes = 0
            with archive.open(member) as member_stream, target.open("wb") as out_file:
                while True:
                    chunk = member_stream.read(_CHUNK)
                    if not chunk:
                        break
                    # Zip-bomb guard 2 of 2 – the bytes that actually arrive.
                    # member.file_size above came from a header the attacker
                    # wrote and is free to understate, so the declared figure is
                    # never trusted on its own.
                    written_bytes += len(chunk)
                    if written_bytes > MAX_EXTRACTED_SIZE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="ZIP contents exceed the allowed extracted size.",
                        )
                    out_file.write(chunk)
            # Report the sanitised path, never member.filename: the caller sends
            # this list to the browser, and echoing an attacker's raw name back
            # into the UI is how a filename becomes stored XSS.
            extracted.append("/".join(safe_parts))

    return extracted


# ─── Main .tex Detection ──────────────────────────────────────────────────────

def detect_main_tex(session_dir: Path) -> str | None:
    """Detect the main ``.tex`` entry point.

    Priority: an exact ``main.tex`` in the root → the only root-level .tex →
    the .tex containing ``\\documentclass`` closest to the root →
    any .tex with ``\\begin{document}`` → the first .tex found.

    A guess, by design: compiling the wrong file is a recoverable annoyance (the
    user picks another from the file tree), whereas refusing to guess would make
    every upload a manual step. The order is cheapest-and-most-certain first, so
    the common cases never open a file at all.
    """
    tex_files = list(session_dir.rglob("*.tex"))
    if not tex_files:
        return None

    root_main = session_dir / "main.tex"
    if root_main.exists():
        return "main.tex"

    root_level_tex = [f for f in tex_files if f.parent == session_dir]
    if len(root_level_tex) == 1:
        return root_level_tex[0].name

    # (file, depth) pairs – depth is how far below the session root the file is.
    documentclass_files: List[Tuple[Path, int]] = []
    for tex in tex_files:
        try:
            # errors="ignore": .tex sources turn up in latin-1 and other legacy
            # encodings. We only look for an ASCII marker, so mojibake elsewhere
            # in the file is irrelevant and must not abort detection.
            text = tex.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if r"\documentclass" in text:
            depth = len(tex.relative_to(session_dir).parts)
            documentclass_files.append((tex, depth))

    if documentclass_files:
        # Shallowest wins, because a main file lives at the top of a project and
        # the deeper \documentclass files are usually standalone figures or
        # unused templates. Shortest name breaks ties, favouring "thesis.tex"
        # over "thesis-appendix-draft.tex".
        documentclass_files.sort(key=lambda entry: (entry[1], len(entry[0].name)))
        return _rel(documentclass_files[0][0], session_dir)

    # Last resorts: a file with a document body but a \documentclass pulled in
    # via \input, then simply the first .tex we found.
    for tex in tex_files:
        try:
            if r"\begin{document}" in tex.read_text(encoding="utf-8", errors="ignore"):
                return _rel(tex, session_dir)
        except OSError:
            continue

    return _rel(tex_files[0], session_dir)


def _rel(path: Path, root: Path) -> str:
    """Return the workspace-relative path with forward slashes.

    Every path that crosses into JSON goes through here so the frontend, the
    URLs it builds and the tests all see one spelling — Windows backslashes
    would otherwise leak into API responses and act as escapes in JS.
    """
    return str(path.relative_to(root)).replace("\\", "/")


def list_session_files(session_dir: Path) -> List[dict]:
    """List the files in a session as ``{name, path, ext, size}`` entries.

    Internal bookkeeping files (the ``.last_access`` marker, temp uploads) are
    hidden from the user.

    Sorted so the file tree keeps a stable order between refreshes instead of
    reshuffling with whatever order the filesystem happens to return.
    """
    result = []
    for item in sorted(session_dir.rglob("*")):
        if not item.is_file():
            continue
        name = item.name
        # Implementation detail, not user content: showing these would invite
        # someone to edit or delete the machinery that keeps the session alive.
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

    Unlike the upload path this does not rewrite the name — the file must keep
    the exact name the user is editing — so every rule here has to reject rather
    than sanitise.
    """
    cleaned_path = filepath.lstrip("/\\")
    if ":" in cleaned_path:  # blocks C:\…, file.tex:$DATA, etc.
        # Rejected anywhere in the string, not just at position 1, and on every
        # platform. Two distinct Windows behaviours hide behind this character:
        #   * a drive prefix — joining "C:\evil" onto the session path makes
        #     pathlib *discard* the session part entirely. The confinement check
        #     below would still catch that one.
        #   * an NTFS alternate data stream — "main.tex:payload" writes a second,
        #     invisible file attached to main.tex. That path really is inside the
        #     session, so relative_to() accepts it happily; this check is the
        #     only thing that stops it.
        raise HTTPException(status_code=400, detail="Invalid path.")

    # resolve() before comparing: it collapses ".." and follows links, so the
    # comparison is against the real destination rather than the text of the
    # request. Comparing the strings first would miss "a/../../b".
    target = (session_dir / cleaned_path).resolve()
    try:
        target.relative_to(session_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal attempt detected.")
    return target


def read_file_content(session_dir: Path, filepath: str) -> bytes:
    """Read a file securely from the session workspace.

    Returns raw bytes rather than text because the same endpoint serves images
    and PDFs alongside .tex sources; decoding is the caller's decision.
    """
    target = _resolve_secure_path(session_dir, filepath)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    # Explicitly reject directories and devices: without this, a request for a
    # folder would surface as an opaque OSError/500 instead of a clear 400.
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Target is not a file.")
    return target.read_bytes()


def write_file_content(session_dir: Path, filepath: str, content: bytes) -> None:
    """Write a file securely into the session workspace.

    Validates the extension and enforces the per-file size limit so a huge
    editor save cannot exhaust memory or disk.

    The extension whitelist is what stops this endpoint from becoming the
    ``.latexmkrc`` hole that ZIP extraction closed: ``Path(".latexmkrc").suffix``
    is ``""``, and the empty extension is deliberately not whitelisted (locked by
    ``test_extension_whitelist_has_no_dangerous_entries``).
    """
    # Size first: the cheapest rejection, and it avoids creating parent
    # directories for a save that is about to be refused anyway.
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_SIZE_MB} MB limit.",
        )
    target = _resolve_secure_path(session_dir, filepath)
    # Validate the resolved basename, not the raw request path, so the check
    # applies to the name actually about to be written.
    _validate_extension(target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
