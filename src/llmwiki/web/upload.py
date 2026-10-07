"""POST /api/upload?space=<space>&filename=<name>  (raw request body = the file bytes).

Writes only to <data_dir>/inbox/<space>/<sanitized-name>; the pipeline picks it up from there.
The client file name is never used as a path: it is validated, sanitized and made unique.
"""
from __future__ import annotations

import os
import secrets
import unicodedata
from pathlib import Path

from ..ingest.image import IMAGE_EXTS, is_image
from ..pipeline.inbox import InvalidSpace, inbox_dir, is_temp_name, validate_space
from .http import HttpError, Request, Response, json_response

ALLOWED_EXT = (".eml", ".md", ".txt", ".docx", ".xlsx", ".pdf", *IMAGE_EXTS)
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
_SAFE_PUNCT = frozenset(" ._-()[]")
_CHUNK = 64 * 1024
_MAX_NAME = 120


def _stem_chars_ok(stem: str) -> bool:
    """Letters (any script, incl. Korean), decimal digits and a few ASCII marks only; no format,
    control, symbol, other-number (superscripts) or combining characters."""
    return all(c in _SAFE_PUNCT or unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd") for c in stem)


def _reserved(stem: str) -> bool:
    return any(unicodedata.normalize("NFKC", seg).lower().strip(" .") in _RESERVED for seg in stem.split("."))


def sanitize_filename(name: str) -> str:
    """Return a safe single-component file name or raise HttpError(400, 'bad_filename')."""
    bad = HttpError(400, "bad_filename")
    if not isinstance(name, str) or not name or len(name) > 255:
        raise bad
    if any(c in name for c in "/\\:\x00") or any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
        raise bad  # path separators, drive letters, NTFS streams, control/bidi characters: reject, never "fix"
    name = unicodedata.normalize("NFC", name)
    if name != name.strip() or name.startswith(".") or name.endswith(".") or is_temp_name(name):
        raise bad
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        raise HttpError(400, "bad_extension")
    stem = stem.strip(" .")[:_MAX_NAME]
    if not stem or not _stem_chars_ok(stem) or _reserved(stem):
        raise bad
    return stem + ext


def _magic_ok(ext: str, head: bytes) -> bool:
    if ext == ".pdf":
        return b"%PDF-" in head[:1024]
    if ext in (".docx", ".xlsx"):
        return head[:2] == b"PK"
    if ext in IMAGE_EXTS:
        return is_image(head)
    return True


def _publish(tmp: Path, dest_dir: Path, name: str) -> Path:
    """Move tmp to dest_dir/name without ever overwriting (adds -2, -3... on collision)."""
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        dest = dest_dir / (name if n == 1 else f"{stem}-{n}{ext}")
        try:
            os.link(tmp, dest)  # atomic and fails if dest exists
            break
        except FileExistsError:
            n += 1
        except OSError:  # filesystem without hard links: exclusive-create copy
            try:
                with open(dest, "xb") as out, open(tmp, "rb") as src:
                    while chunk := src.read(_CHUNK):
                        out.write(chunk)
                break
            except FileExistsError:
                n += 1
    tmp.unlink(missing_ok=True)
    return dest


def handle_upload(app, req: Request, session) -> Response:
    user, audit = session.user, app.audit
    space, filename = req.query.get("space", ""), req.query.get("filename", "")
    try:
        validate_space(space)
    except InvalidSpace:
        raise HttpError(400, "bad_space") from None
    if space not in user.spaces:
        audit.record(user, "upload_denied", space)
        raise HttpError(403, "forbidden")
    name = sanitize_filename(filename)
    length = req.content_length
    if length > app.cfg.max_upload_bytes:
        raise HttpError(413, "too_large")
    if length == 0:
        raise HttpError(400, "empty_file")

    dest_dir = inbox_dir(app.settings).joinpath(*space.split("/"))
    root = inbox_dir(app.settings).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_dir.resolve().is_relative_to(root):
        raise HttpError(400, "bad_space")
    tmp = dest_dir / f".upload-{secrets.token_hex(8)}.part"  # dot-prefixed: the pipeline ignores it
    stream, remaining, head = req.environ["wsgi.input"], length, b""
    try:
        with open(tmp, "xb") as f:
            while remaining:
                try:
                    chunk = stream.read(min(_CHUNK, remaining))
                except OSError:  # socket timeout / reset: the finally below removes the .part file
                    raise HttpError(408, "request_timeout") from None
                if not chunk:
                    raise HttpError(400, "truncated_body")
                if not head:
                    head = chunk[:1024]
                remaining -= len(chunk)
                f.write(chunk)
        if not _magic_ok(os.path.splitext(name)[1], head):
            raise HttpError(400, "bad_content")
        final = _publish(tmp, dest_dir, name)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        audit.record(user, "upload", f"{space}/{final.name}", f"bytes={length}")
    except Exception:
        final.unlink(missing_ok=True)  # no unaudited files in the inbox
        raise
    return json_response(201, {"ok": True, "space": space, "name": final.name})
