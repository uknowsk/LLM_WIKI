"""EML parser (stdlib `email`). Attachments are returned separately for independent ingest."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath

from llmwiki.ingest.limits import check_size
from llmwiki.models import ParsedDocument

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "ul", "ol", "section", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
               "table", "blockquote"}
_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"script", "style", "head", "title"}
_CHARSET_ALIASES = {"ks_c_5601-1987": "cp949", "ks_c_5601": "cp949", "euc-kr": "cp949", "euckr": "cp949"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "body":
            self._skip = 0  # an unclosed <head>/<title> must not swallow the body
        elif tag in _SKIP_TAGS:
            self._skip += 1
        elif self._skip:
            return
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append("\t")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip HTML to readable plain text. Table cells are tab-separated, block tags break lines."""
    p = _HtmlToText()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@dataclass(frozen=True)
class EmlResult:
    """Parsed mail plus its attachments as (filename, bytes) for separate ingestion."""

    document: ParsedDocument
    attachments: list[tuple[str, bytes]] = field(default_factory=list)


def _part_text(part: EmailMessage) -> str:
    """Decode strictly from the raw payload: declared charset, utf-8, cp949, then replace (never silent mojibake)."""
    payload = part.get_payload(decode=True) or b""
    declared = (part.get_content_charset() or "").lower()
    encodings = ["utf-8", "cp949"]
    if declared:
        encodings.insert(0, _CHARSET_ALIASES.get(declared, declared))
    for enc in encodings:
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = _part_text(plain)
        if text.strip():
            return text.strip()
    html = msg.get_body(preferencelist=("html",))
    return html_to_text(_part_text(html)) if html is not None else ""


def _addr_list(msg: EmailMessage, header: str) -> str:
    return ", ".join(str(v) for v in msg.get_all(header, []))


def _ids(value: str) -> str:
    return " ".join(re.findall(r"<[^<>\s]+>", value))


def _unique_name(name: str | None, used: set[str]) -> str:
    """Safe basename, unique (case-insensitively) within the mail: attachment-1.bin, a.pdf, a-2.pdf ..."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    if base in ("", ".", ".."):
        n = 1
        while f"attachment-{n}.bin" in used:
            n += 1
        base = f"attachment-{n}.bin"
    cand, p, n = base, PurePath(base), 1
    while cand.lower() in used:
        n += 1
        cand = f"{p.stem}-{n}{p.suffix}"
    used.add(cand.lower())
    return cand


def parse_eml(data: bytes, source_name: str, max_bytes: int | None = None) -> EmlResult:
    """Parse raw .eml bytes. Metadata keys: date (YYYY-MM-DD), from, to, cc, message_id, in_reply_to, references.

    Nested message/rfc822 parts are returned as text attachments (forwarded-N.txt) so no body is lost.
    """
    check_size(data, source_name, max_bytes)
    msg = message_from_bytes(data, policy=policy.default)
    meta: dict[str, str] = {}
    raw_date = msg.get("Date")
    if raw_date:
        try:
            meta["date"] = parsedate_to_datetime(str(raw_date)).date().isoformat()
        except (TypeError, ValueError):
            pass
    for key, header in (("from", "From"), ("to", "To"), ("cc", "Cc")):
        val = _addr_list(msg, header)
        if val:
            meta[key] = val
    for key, header in (("message_id", "Message-ID"), ("in_reply_to", "In-Reply-To"), ("references", "References")):
        val = _ids(" ".join(str(v) for v in msg.get_all(header, [])))
        if val:
            meta[key] = val
    attachments: list[tuple[str, bytes]] = []
    used: set[str] = set()
    nested_n = 0
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is not None:
                nested_n += 1
                head = f"Subject: {inner.get('Subject', '')}\nFrom: {inner.get('From', '')}\n\n"
                text = head + _body_text(inner)
                attachments.append((_unique_name(f"forwarded-{nested_n}.txt", used), text.encode("utf-8")))
            continue
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            attachments.append((_unique_name(name, used), payload))
    subject = str(msg.get("Subject") or "").strip() or PurePath(source_name).stem
    doc = ParsedDocument(title=subject, text=_body_text(msg), source_name=source_name, metadata=meta)
    return EmlResult(document=doc, attachments=attachments)


def thread_key(metadata: dict[str, str]) -> str:
    """Stable thread id: first Reference (thread root), else In-Reply-To, else own Message-ID."""
    for key in ("references", "in_reply_to", "message_id"):
        ids = re.findall(r"<([^<>\s]+)>", metadata.get(key, ""))
        if ids:
            return ids[0].lower()
    return ""
