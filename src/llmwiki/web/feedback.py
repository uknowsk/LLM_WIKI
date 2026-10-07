"""POST /api/feedback  {"rating": "up"|"down", "paths": [<cited article paths>]}: was the answer helpful?

Recorded as one audit row per article (action feedback_up / feedback_down, target = the article path); the question and
the answer are never stored. Only articles the user may read are accepted; one unreadable or unknown path rejects the
whole request with the same 404 as a missing article, so this cannot be used to probe which articles exist.
"""
from __future__ import annotations

from .api import query_service
from .http import HttpError, Request, Response, json_response

MAX_PATHS = 8
MAX_PATH = 512
NO_PATH = "-"


def handle_feedback(app, req: Request, session) -> Response:
    user, audit = session.user, app.audit
    body = req.read_json(app.cfg.max_json_bytes)
    rating, paths = body.get("rating"), body.get("paths")
    if rating not in ("up", "down") or not isinstance(paths, list) or len(paths) > MAX_PATHS \
            or not all(isinstance(p, str) and 0 < len(p) <= MAX_PATH for p in paths):
        raise HttpError(400, "bad_feedback")
    paths = list(dict.fromkeys(paths))
    service = query_service(app)
    for p in paths:
        if not service.can_open(user, p):
            audit.record(user, "feedback_denied", p)
            raise HttpError(404, "not_found")  # identical for denied and missing
    for p in paths or [NO_PATH]:
        audit.record(user, f"feedback_{rating}", p)
    return json_response(200, {"ok": True})
