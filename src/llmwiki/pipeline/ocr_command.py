"""OCR through an external executable (no shell, argument list, timeout, output cap).

Contract for the external command (WIKI_OCR_COMMAND, optional JSON list WIKI_OCR_ARGS appended):
  * WIKI_OCR_INPUT=pdf (default): the WHOLE PDF is written to stdin; the command prints the UTF-8 text of
    every page to stdout, pages separated by a form feed (\\f), exactly one segment per page, in order.
    (The OcrEngine protocol hands over PDF bytes + a page index; this package does not rasterize pages.)
  * WIKI_OCR_INPUT=image: stdin is one PNG/JPEG, stdout is its UTF-8 text. Only used when the caller
    passes image bytes to ocr_page()/ocr_image().
  The child sees env var WIKI_OCR_INPUT with the kind actually sent. Exit code != 0 is an error.
  Other WIKI_* variables (secrets) are NOT passed to the child; stderr content is never logged or returned.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT = 8 * 1024 * 1024
_PNG, _JPEG = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
BLANK_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


class OcrError(RuntimeError):
    """Never contains child stderr or document content."""


def _is_image(data: bytes) -> bool:
    return data.startswith(_PNG) or data.startswith(_JPEG)


class CommandOcrEngine:
    def __init__(self, command: str, args: list[str] | None = None, *, input_kind: str = "pdf",
                 timeout: float = DEFAULT_TIMEOUT, max_output: int = DEFAULT_MAX_OUTPUT):
        if input_kind not in ("pdf", "image"):
            raise ValueError("WIKI_OCR_INPUT must be 'pdf' or 'image'")
        if timeout <= 0 or max_output <= 0:
            raise ValueError("OCR timeout and output cap must be positive")
        self.command, self.args = command, list(args or [])
        self.input_kind, self.timeout, self.max_output = input_kind, timeout, max_output
        self._lock = threading.Lock()
        self._cache: tuple[str, list[str]] | None = None

    # ---- OcrEngine protocol -------------------------------------------------------------------
    def ocr_page(self, data: bytes, page_index: int) -> str:
        if _is_image(data):
            return self.ocr_image(data)
        if self.input_kind != "pdf":
            raise OcrError("WIKI_OCR_INPUT=image cannot OCR a PDF (pages are not rasterized here); use WIKI_OCR_INPUT=pdf")
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:  # one child per PDF, then served page by page
            if self._cache is None or self._cache[0] != digest:
                self._cache = (digest, self._run(data, "pdf").split("\f"))
            pages = self._cache[1]
        if page_index >= len(pages):
            raise OcrError(f"OCR command returned {len(pages)} page segment(s); page {page_index + 1} requested")
        return pages[page_index]

    def ocr_image(self, data: bytes) -> str:
        return self._run(data, "image")

    def probe(self) -> str:
        """Self-test with a 1x1 PNG (image mode) or a blank 1-page PDF (pdf mode). Returns the text."""
        return self._run(PNG_1X1, "image") if self.input_kind == "image" else self._run(BLANK_PDF, "pdf")

    # ---- subprocess ---------------------------------------------------------------------------
    def _run(self, payload: bytes, kind: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WIKI_")}
        env["WIKI_OCR_INPUT"] = kind
        try:
            proc = subprocess.Popen([self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, shell=False)
        except OSError as exc:
            raise OcrError(f"cannot start OCR command ({type(exc).__name__})") from None
        out, over, err_bytes = bytearray(), threading.Event(), [0]

        def feed() -> None:
            try:
                proc.stdin.write(payload)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        def read_out() -> None:
            while chunk := proc.stdout.read(65536):
                out.extend(chunk)
                if len(out) > self.max_output:
                    over.set()
                    proc.kill()
                    return

        def drain_err() -> None:
            while chunk := proc.stderr.read(65536):  # content discarded on purpose
                err_bytes[0] += len(chunk)

        threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, drain_err)]
        for t in threads:
            t.start()
        try:
            code = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise OcrError(f"OCR command timed out after {self.timeout:g}s") from None
        finally:
            for t in threads:
                t.join(5)
            for s in (proc.stdout, proc.stderr):
                s.close()
        if over.is_set():
            raise OcrError(f"OCR output exceeds the {self.max_output} byte cap")
        if code != 0:
            raise OcrError(f"OCR command exited with code {code} (stderr {err_bytes[0]} bytes, not shown)")
        try:
            return bytes(out).decode("utf-8")
        except UnicodeDecodeError:
            raise OcrError("OCR command output is not valid UTF-8") from None


def ocr_from_env(environ: Mapping[str, str] | None = None) -> CommandOcrEngine | None:
    """None when WIKI_OCR_COMMAND is unset. Raises ValueError on a bad configuration (fail closed)."""
    e = os.environ if environ is None else environ
    cmd = (e.get("WIKI_OCR_COMMAND") or "").strip()
    if not cmd:
        return None
    if not (Path(cmd).is_file() or shutil.which(cmd)):
        raise ValueError("WIKI_OCR_COMMAND does not point to an existing executable")
    args: list[str] = []
    raw = (e.get("WIKI_OCR_ARGS") or "").strip()
    if raw:
        try:
            args = json.loads(raw)
        except ValueError:
            raise ValueError("WIKI_OCR_ARGS must be a JSON list of strings") from None
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("WIKI_OCR_ARGS must be a JSON list of strings")

    def num(key: str, default: float) -> float:
        v = (e.get(key) or "").strip()
        if not v:
            return default
        try:
            n = float(v)
        except ValueError:
            raise ValueError(f"{key} must be a number") from None
        if n <= 0:
            raise ValueError(f"{key} must be positive")
        return n

    return CommandOcrEngine(cmd, args, input_kind=(e.get("WIKI_OCR_INPUT") or "pdf").strip().lower(),
                            timeout=num("WIKI_OCR_TIMEOUT", DEFAULT_TIMEOUT),
                            max_output=int(num("WIKI_OCR_MAX_OUTPUT", DEFAULT_MAX_OUTPUT)))
