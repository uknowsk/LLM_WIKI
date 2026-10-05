"""JSON API handlers. Authorization decisions stay in the engine (QueryService); nothing is re-implemented here."""
from __future__ import annotations

import logging

from ..engine import article as art
from ..engine.llm import LLMError
from ..engine.query import AccessDenied, QueryService
from .http import HttpError, Request, json_response
from .render import render_markdown

log = logging.getLogger("llmwiki.web")
MAX_QUESTION = 2000
MAX_PATH = 512
AUDIT_LIMIT = 1000


def user_payload(user, csrf: str) -> dict:
    return {"id": user.id, "name": user.name, "department": user.department, "part": user.part,
            "spaces": sorted(user.spaces), "is_admin": user.is_admin, "csrf": csrf}


def healthz(app, req: Request, session):
    return json_response(200, {"status": "ok"})


def me(app, req: Request, session):
    body = user_payload(session.user, session.csrf)
    if app.cfg.personal:
        body["personal"] = True  # the UI hides logout / the space selector and shows the status line
    return json_response(200, body)


def query(app, req: Request, session):
    body = req.read_json(app.cfg.max_json_bytes)
    q = body.get("question")
    if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUESTION:
        raise HttpError(400, "bad_question")
    service = QueryService(app.settings, app.store, app.llm, app.audit, embedder=app.embedder)
    try:
        res = service.query(session.user, q.strip())
    except LLMError as exc:
        if getattr(exc, "busy", False):  # personal mode: waited too long behind document processing
            raise HttpError(503, "llm_busy") from None
        log.error("llm unavailable")
        raise HttpError(502, "llm_unavailable") from None
    cites = [{"path": p, "title": app.store.article_title(p) or p} for p in res.citations]
    return json_response(200, {"answer": res.answer, "citations": cites})


def article(app, req: Request, session):
    path = req.query.get("path", "")
    service = QueryService(app.settings, app.store, app.llm, app.audit)
    try:
        if len(path) > MAX_PATH:
            raise AccessDenied(path)
        text = service.read_article(session.user, path)
    except AccessDenied:
        raise HttpError(404, "not_found") from None  # identical for denied and missing
    parsed = art.parse(path, text)
    return json_response(200, {"path": path, "title": parsed.title, "updated": parsed.updated,
                               "html": render_markdown(parsed.body)})


def audit_entries(app, req: Request, session):
    if not session.user.is_admin:
        raise HttpError(403, "forbidden")
    uid = req.query.get("user_id") or None
    if uid is not None and len(uid) > 256:
        raise HttpError(400, "bad_request")
    rows = app.audit.entries(uid)[-AUDIT_LIMIT:]
    app.audit.record(session.user, "audit_view", uid or "*")
    keys = ("ts", "user_id", "action", "target", "detail")
    return json_response(200, {"entries": [dict(zip(keys, r)) for r in rows]})
