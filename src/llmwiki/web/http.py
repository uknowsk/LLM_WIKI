"""Minimal request/response helpers for the WSGI app."""
from __future__ import annotations

import http
import json
from urllib.parse import parse_qs


class HttpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class Response:
    def __init__(self, status: int, body: bytes, content_type: str, headers: list[tuple[str, str]] | None = None):
        self.status, self.body = status, body
        self.headers = [("Content-Type", content_type), *(headers or [])]

    def add(self, name: str, value: str) -> "Response":
        self.headers.append((name, value))
        return self

    @property
    def status_line(self) -> str:
        return f"{self.status} {http.HTTPStatus(self.status).phrase}"


def json_response(status: int, obj, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", headers)


def error_response(status: int, code: str) -> Response:
    return json_response(status, {"error": code})


class Request:
    def __init__(self, environ: dict):
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.bad_query = False
        try:
            q = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True, max_num_fields=20)
        except ValueError:  # too many fields: reported as 400 by the app's normal error path
            q, self.bad_query = {}, True
        self.query = {k: v[0] for k, v in q.items()}

    def header(self, name: str) -> str | None:
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self.environ.get(key) or None
        return self.environ.get("HTTP_" + key)

    @property
    def content_length(self) -> int:
        raw = (self.environ.get("CONTENT_LENGTH") or "").strip()
        if not raw:
            raise HttpError(411, "length_required")
        if not raw.isascii() or not raw.isdigit():
            raise HttpError(400, "bad_request")
        if len(raw) > 12:  # avoids int() on absurd digit strings; 12 digits is far beyond any limit
            raise HttpError(413, "too_large")
        return int(raw)

    def read_json(self, max_bytes: int) -> dict:
        if (self.header("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415, "unsupported_media_type")
        n = self.content_length
        if n > max_bytes:
            raise HttpError(413, "too_large")
        try:
            raw = self.environ["wsgi.input"].read(n)
        except OSError:  # includes socket timeouts (slow client)
            raise HttpError(408, "request_timeout") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HttpError(400, "bad_json") from None
        if not isinstance(data, dict):
            raise HttpError(400, "bad_json")
        return data

    def read_form(self, max_bytes: int, max_fields: int = 50) -> dict[str, list[str]]:
        """application/x-www-form-urlencoded body -> {name: [values]}. 415 wrong type, 411/400 bad length,
        413 too large, 408 slow client, 400 malformed (non-ASCII body, bad pairs, invalid UTF-8, too many fields)."""
        if (self.header("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            raise HttpError(415, "unsupported_media_type")
        n = self.content_length
        if n > max_bytes:
            raise HttpError(413, "too_large")
        try:
            raw = self.environ["wsgi.input"].read(n)
        except OSError:
            raise HttpError(408, "request_timeout") from None
        if len(raw) != n:
            raise HttpError(400, "bad_request")
        try:
            return parse_qs(raw.decode("ascii"), keep_blank_values=True, strict_parsing=bool(raw),
                            encoding="utf-8", errors="strict", max_num_fields=max_fields)
        except (ValueError, UnicodeError):
            raise HttpError(400, "bad_request") from None
