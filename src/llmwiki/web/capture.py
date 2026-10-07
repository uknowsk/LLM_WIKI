"""POST /api/capture  {"space", "text", "title"?, "url"?}: a quick note or web clipping into the user's inbox.

Same trust rules as upload: the space must be one of the user's own, the client never chooses a path (the file name is
built from a cleaned title and made unique), and every capture is audited. The URL is only recorded as text in the
note; the server never fetches it. The note lands in inbox/<space>/ and the normal pipeline compiles it.
"""
from __future__ import annotations

import secrets
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from ..pipeline.inbox import InvalidSpace, inbox_dir, validate_space
from .http import HttpError, Request, Response, json_response
from .upload import _SAFE_PUNCT, _publish, sanitize_filename

MAX_BYTES = 1024 * 1024
MAX_TITLE = 60
MAX_URL = 2000
DEFAULT_TITLE = "메모"


def _clean_title(title: str) -> str:
    """Letters/digits/a few marks only; anything else (separators, control, bidi, symbols) becomes a space."""
    kept = "".join(c if (c in _SAFE_PUNCT or unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nd")) else " "
                   for c in unicodedata.normalize("NFC", title))
    return " ".join(kept.split()).strip(" .")[:MAX_TITLE].strip(" .")


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf")).strip()


def _check_url(url: str) -> str:
    bad = HttpError(400, "bad_url")
    if not url:
        return ""
    if len(url) > MAX_URL or any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in url):
        raise bad
    try:
        parts = urlsplit(url)
    except ValueError:
        raise bad from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise bad
    return url


def _text_field(body: dict, key: str) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise HttpError(400, "bad_request")
    return v


def handle_capture(app, req: Request, session) -> Response:
    user, audit = session.user, app.audit
    body = req.read_json(min(app.cfg.max_upload_bytes, MAX_BYTES))
    space = body.get("space")
    try:
        if not isinstance(space, str):
            raise InvalidSpace(str(type(space)))
        validate_space(space)
    except InvalidSpace:
        raise HttpError(400, "bad_space") from None
    if space not in user.spaces:
        audit.record(user, "capture_denied", space)
        raise HttpError(403, "forbidden")
    title, url, text = _text_field(body, "title"), _text_field(body, "url").strip(), _text_field(body, "text")
    url = _check_url(url)
    text = _clean_text(text)
    if not text:
        raise HttpError(400, "empty_text")
    shown = _clean_title(title) or _clean_title(text.split("\n", 1)[0]) or DEFAULT_TITLE
    day = datetime.fromtimestamp(app._clock()).strftime("%Y-%m-%d")
    try:
        name = sanitize_filename(f"{day} {shown}.md")
    except HttpError:  # e.g. a reserved device name: fall back to the neutral title
        name = sanitize_filename(f"{day} {DEFAULT_TITLE}.md")
    lines = [f"# {shown}", ""]
    if url:
        lines += [f"출처 URL: {url}", ""]
    data = ("\n".join(lines) + text + "\n").encode("utf-8")

    dest_dir = inbox_dir(app.settings).joinpath(*space.split("/"))
    root = inbox_dir(app.settings).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_dir.resolve().is_relative_to(root):
        raise HttpError(400, "bad_space")
    tmp = dest_dir / f".capture-{secrets.token_hex(8)}.part"  # dot-prefixed: the pipeline ignores it
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        final = _publish(tmp, dest_dir, name)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        audit.record(user, "capture", f"{space}/{final.name}", f"bytes={len(data)}")
    except Exception:
        final.unlink(missing_ok=True)  # no unaudited files in the inbox
        raise
    return json_response(201, {"ok": True, "space": space, "name": final.name})
