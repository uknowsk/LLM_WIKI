"""Read-only personal endpoints (registered only by the personal entry point; the central server has no such route)."""
from __future__ import annotations

from ..web.http import json_response


def make_status_handler(worker, gate):
    def status(app, req, session):
        return json_response(200, {**worker.status(), "busy": bool(gate.background_active)})
    return status


def make_info_handler(home: str, watch: list[str]):
    def info(app, req, session):
        return json_response(200, {"home": home, "wiki": str(app.settings.wiki_dir), "watch": watch})
    return info
