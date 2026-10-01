"""EML parser (stdlib `email`). Attachments are returned separately for independent ingest."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath

from llmwiki.models import ParsedDocument

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip HTML to readable plain text."""
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
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):  # unknown/mislabeled charset
        payload = part.get_payload(decode=True) or b""
        for enc in ("utf-8", "cp949"):
            try:
                return payload.decode(enc)
            except UnicodeDecodeError:
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


def parse_eml(data: bytes, source_name: str) -> EmlResult:
    """Parse raw .eml bytes. Metadata keys: date (YYYY-MM-DD), from, to, cc, message_id, in_reply_to, references."""
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
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            attachments.append((PurePath(name or "attachment.bin").name, payload))
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
