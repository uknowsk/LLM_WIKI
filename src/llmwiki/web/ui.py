"""Single-page Korean UI. Served as three same-origin resources so the CSP needs no 'unsafe-inline'."""
from __future__ import annotations

from .http import Response
from .ui_js import APP_JS

PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>사내 위키</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<div id="app"></div>
<script src="/static/app.js" defer></script>
</body>
</html>
"""

APP_CSS = """
body { font-family: "Malgun Gothic", system-ui, sans-serif; margin: 0; background: #f4f5f7; color: #1d2433; }
header { display: flex; justify-content: space-between; align-items: center; padding: 10px 20px; background: #1f3a5f; color: #fff; }
header button { margin-left: 8px; }
main { max-width: 900px; margin: 20px auto; padding: 0 16px; }
nav { display: flex; gap: 8px; margin-bottom: 16px; }
nav button.active { background: #1f3a5f; color: #fff; }
section { background: #fff; border-radius: 8px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
textarea, input, select { width: 100%; box-sizing: border-box; padding: 8px; margin: 6px 0; font: inherit; }
button { padding: 8px 14px; border: 1px solid #1f3a5f; background: #fff; color: #1f3a5f; border-radius: 4px; cursor: pointer; font: inherit; }
button.primary { background: #1f3a5f; color: #fff; }
.answer { white-space: pre-wrap; margin: 12px 0; }
.cites button { margin: 0 6px 6px 0; }
.error { color: #b00020; }
.ok { color: #1b6e2d; }
.muted { color: #667; font-size: 90%; }
pre { background: #eef0f4; padding: 8px; overflow: auto; }
"""


def _resp(body: str, ctype: str) -> Response:
    return Response(200, body.encode("utf-8"), ctype)


def page(app, req, session) -> Response:
    return _resp(PAGE, "text/html; charset=utf-8")


def script(app, req, session) -> Response:
    return _resp(APP_JS, "text/javascript; charset=utf-8")


def style(app, req, session) -> Response:
    return _resp(APP_CSS, "text/css; charset=utf-8")
