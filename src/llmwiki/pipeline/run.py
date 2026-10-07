"""process_file: parse -> save_raw (PII policy) -> register -> compile, one file at a time.

One bad file never raises out of process_file; the outcome is returned and audited.
"""
from __future__ import annotations

import hashlib
import sqlite3
from email import message_from_bytes
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePath

from llmwiki.audit import AuditLog
from llmwiki.auth import User
from llmwiki.config import Settings
from llmwiki.engine.compile import Compiler
from llmwiki.engine.embed import Embedder
from llmwiki.engine.llm import LLMClient
from llmwiki.engine.store import Store
from llmwiki.ingest.docx import parse_docx
from llmwiki.ingest.limits import max_input_bytes
from llmwiki.ingest.xlsx import parse_xlsx
from llmwiki.ingest.eml import parse_eml
from llmwiki.ingest.image import IMAGE_EXTS, parse_image
from llmwiki.ingest.pdf import OcrEngine, PdfExtractor, parse_pdf
from llmwiki.ingest.save import save_raw
from llmwiki.ingest.text import parse_text
from llmwiki.models import ParsedDocument, RawRecord
from llmwiki.pipeline import inbox, notes
from llmwiki.pipeline.aliases import AliasMap

SYSTEM_USER = User(id="system:pipeline", name="pipeline", department="system")
SUPPORTED = (".eml", ".md", ".txt", ".docx", ".xlsx", ".pdf", *IMAGE_EXTS)
# no nested mails, and no images inside mails (signature logos/banners would each cost an OCR call or an error)
ATTACHMENT_SUPPORTED = tuple(e for e in SUPPORTED if e != ".eml" and e not in IMAGE_EXTS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_sources (
    space TEXT NOT NULL, sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, parent_raw TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (space, sha256)
)
"""


@dataclass
class ProcessResult:
    status: str  # done | skipped | rejected | failed | locked (in use or still growing: retried, not an attempt)
    path: Path
    space: str
    raw_paths: list[str] = field(default_factory=list)
    articles: list[str] = field(default_factory=list)
    error: str = ""
    attachment_errors: list[str] = field(default_factory=list)


class _Reject(Exception):
    def __init__(self, msg: str, category: str = notes.UNSUPPORTED):
        super().__init__(msg)
        self.category = category


class _Unsafe(Exception):
    """The path runs through a symlink/junction: never read, move or write next to it."""


def mask_for_space(settings: Settings, space: str, policy: Mapping[str, bool] | None) -> bool:
    """Per-space masking policy: nearest ancestor entry wins, else the global default."""
    parts = space.split("/")
    for i in range(len(parts), 0, -1):
        key = "/".join(parts[:i])
        if policy and key in policy:
            return bool(policy[key])
    return settings.mask_pii_default


def _db(settings: Settings) -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(settings.db_path), timeout=30)
    db.execute(_SCHEMA)
    db.commit()
    return db


def _already_ingested(db: sqlite3.Connection, space: str, sha: str) -> bool:
    """Seen as a pipeline source, or (spec) already present in raw_files for that space."""
    for table in ("pipeline_sources", "raw_files"):
        try:
            if db.execute(f"SELECT 1 FROM {table} WHERE space = ? AND sha256 = ?", (space, sha)).fetchone():
                return True
        except sqlite3.OperationalError:  # raw_files not created yet
            continue
    return False


def _parse(data: bytes, name: str, ocr: OcrEngine | None, extractor: PdfExtractor | None) -> ParsedDocument:
    ext = PurePath(name).suffix.lower()
    if ext in (".md", ".txt"):
        return parse_text(data, name)
    if ext == ".docx":
        return parse_docx(data, name)
    if ext == ".xlsx":
        return parse_xlsx(data, name)
    if ext == ".pdf":
        return parse_pdf(data, name, extractor=extractor, ocr=ocr)
    if ext in IMAGE_EXTS:
        return parse_image(data, name, ocr=ocr)
    raise _Reject(f"unsupported file type: {ext or '(none)'}")


def _ingest_doc(
    parsed: ParsedDocument, space: str, settings: Settings, compiler: Compiler, store: Store,
    policy: Mapping[str, bool] | None, db: sqlite3.Connection, audit: AuditLog,
) -> tuple[RawRecord, str | None]:
    """save_raw first (the engine reads the raw file), then register and compile. Undo raw on failure."""
    rec = save_raw(parsed, space, settings, mask_pii=mask_for_space(settings, space, policy))
    raw_file = settings.data_dir / rec.raw_path
    # save_raw returns an existing record for identical content; never undo a raw that was already registered
    pre_existing = db.execute("SELECT 1 FROM raw_files WHERE raw_path = ?", (rec.raw_path,)).fetchone() is not None
    try:
        store.add_raw(rec)
        res = compiler.compile(rec, raw_file.read_text(encoding="utf-8"))
    except Exception:
        if not pre_existing:
            # no half-registered raw: it would be re-saved as "-2" on retry and linger as an orphan source
            db.execute(
                "DELETE FROM raw_files WHERE raw_path = ? AND raw_path NOT IN (SELECT raw_path FROM article_sources)",
                (rec.raw_path,),
            )
            db.commit()
            raw_file.unlink(missing_ok=True)
        raise
    audit.record(SYSTEM_USER, "compile", rec.raw_path, f"{res.decision} -> {res.article or '-'} suspects={res.suspects}")
    return rec, res.article


def _remember(db: sqlite3.Connection, space: str, sha: str, raw_path: str, parent: str = "") -> None:
    db.execute("INSERT OR IGNORE INTO pipeline_sources VALUES (?, ?, ?, ?)", (space, sha, raw_path, parent))
    db.commit()


def _archive(settings: Settings, path: Path, space: str, policy: Mapping[str, bool] | None) -> None:
    """Move to _done. With masking on, the original (unmasked) must not survive: leave a tombstone."""
    if mask_for_space(settings, space, policy):
        inbox.tombstone(settings, path, space, "original removed after ingest because PII masking is on")
    else:
        inbox.mark_done(settings, path, space)


def process_file(
    path: Path | str,
    space: str,
    settings: Settings,
    llm: LLMClient,
    store: Store,
    audit: AuditLog,
    *,
    ocr: OcrEngine | None = None,
    extractor: PdfExtractor | None = None,
    mask_policy: Mapping[str, bool] | None = None,
    embedder: Embedder | None = None,
    aliases: AliasMap | None = None,
    verify_folder: bool = True,
) -> ProcessResult:
    """Ingest one file. Never raises for file-level problems; see ProcessResult.status.

    verify_folder=True (default, fail closed): the job's `space` is re-derived from the inbox folder (+ aliases) and a
    mismatch is rejected without moving anything. Callers must opt OUT explicitly (personal mode), where one fixed space covers all subfolders."""
    path = Path(path)
    result = ProcessResult("failed", path, space)
    db = _db(settings)
    label = f"{space}/{path.name}"
    try:
        try:
            result.space = space = inbox.validate_space(space)
            label = f"{space}/{path.name}"
            if path.suffix.lower() not in SUPPORTED:
                raise _Reject(f"unsupported file type: {path.suffix.lower() or '(none)'}")
            if inbox.has_link_component(settings, path):
                raise _Unsafe("path goes through a symlink/junction")
            if verify_folder:  # never trust the space of a queued job: re-derive it like the scanner does
                try:
                    derived = inbox.space_for_path(settings, path, aliases)
                except inbox.InvalidSpace:
                    raise _Unsafe("folder is not a valid space") from None
                if derived is not None and derived != space:
                    raise _Unsafe("job space does not match the folder")
            before = path.stat()
            if before.st_size > max_input_bytes():
                raise _Reject(f"file too large (> {max_input_bytes()} bytes)", notes.TOO_LARGE)  # checked before reading
            try:
                data = path.read_bytes()
            except PermissionError as exc:
                if not inbox.is_sharing_violation(exc):
                    raise  # access denied is a normal failed attempt (3 strikes -> _failed + note)
                result.status, result.error = "locked", "file is in use"  # Office/copy still holds it: retried
                return result
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != before.st_size:
                result.status, result.error = "locked", "file is still being written"
                return result
            sha = hashlib.sha256(data).hexdigest()
            if _already_ingested(db, space, sha):
                result.status = "skipped"
                audit.record(SYSTEM_USER, "ingest_skip", label, f"duplicate sha256={sha[:12]}")
                _archive(settings, path, space, mask_policy)
                inbox.clear_notice(settings, path)
                return result
            compiler = Compiler(settings, store, llm, embedder=embedder)
            if path.suffix.lower() == ".eml":
                _process_eml(data, path, space, settings, compiler, store, db, audit, mask_policy, ocr, extractor, sha, result)
            else:
                parsed = _parse(data, path.name, ocr, extractor)
                if not parsed.text.strip():
                    raise ValueError("no extractable text (scanned PDF without OCR?)")
                rec, article = _ingest_doc(parsed, space, settings, compiler, store, mask_policy, db, audit)
                _remember(db, space, sha, rec.raw_path)
                result.raw_paths.append(rec.raw_path)
                result.articles += [article] if article else []
            result.status = "done"
            audit.record(SYSTEM_USER, "ingest", label, "raw=" + ",".join(result.raw_paths))
            _archive(settings, path, space, mask_policy)
            inbox.clear_notice(settings, path)
        except _Unsafe as exc:  # nothing is moved: the file may live in another department's folder
            result.status, result.error = "rejected", str(exc)
            audit.record(SYSTEM_USER, "ingest_reject", "(path check)", result.error)  # no names in the audit
        except (_Reject, inbox.InvalidSpace) as exc:
            result.status, result.error = "rejected", str(exc)
            audit.record(SYSTEM_USER, "ingest_reject", label, result.error)
            inbox.reject(settings, path, None if isinstance(exc, inbox.InvalidSpace) else space, result.error)
            inbox.notify(settings, path, exc.category if isinstance(exc, _Reject) else notes.LOCATION)
        except Exception as exc:  # one bad file (parser, LLM JSON, I/O) must not stop the batch
            # exception text can echo document fragments: keep only the class name and the category
            result.status = "failed"
            result.error = f"{type(exc).__name__} ({notes.categorize_error(f'{type(exc).__name__}: {exc}')})"
            audit.record(SYSTEM_USER, "ingest_fail", label, result.error)
    finally:
        db.close()
    return result


def _process_eml(
    data: bytes, path: Path, space: str, settings: Settings, compiler: Compiler, store: Store,
    db: sqlite3.Connection, audit: AuditLog, policy, ocr, extractor, sha: str, result: ProcessResult,
) -> None:
    if not any(message_from_bytes(data).get(h) for h in ("From", "Subject", "Message-ID", "Date")):
        raise ValueError("malformed eml: no mail headers")
    mail = parse_eml(data, path.name)
    rec, article = _ingest_doc(mail.document, space, settings, compiler, store, policy, db, audit)
    _remember(db, space, sha, rec.raw_path)
    result.raw_paths.append(rec.raw_path)
    result.articles += [article] if article else []
    for name, payload in mail.attachments:
        try:
            if PurePath(name).suffix.lower() not in ATTACHMENT_SUPPORTED:
                raise _Reject(f"unsupported attachment type: {PurePath(name).suffix.lower() or '(none)'}")
            asha = hashlib.sha256(payload).hexdigest()
            if _already_ingested(db, space, asha):
                continue
            parsed = _parse(payload, name, ocr, extractor)
            if not parsed.text.strip():
                raise ValueError("no extractable text")
            # link to the parent mail: kept in the raw header's Source line and in pipeline_sources
            parsed = replace(parsed, source_name=f"{name} (attachment of {rec.raw_path})",
                             metadata={**parsed.metadata, "parent": rec.raw_path})
            arec, aart = _ingest_doc(parsed, space, settings, compiler, store, policy, db, audit)
            _remember(db, space, asha, arec.raw_path, rec.raw_path)
            result.raw_paths.append(arec.raw_path)
            result.articles += [aart] if aart else []
        except Exception as exc:  # an attachment problem must not undo the already-compiled mail
            msg = f"{name}: {type(exc).__name__} ({notes.categorize_error(f'{type(exc).__name__}: {exc}')})"
            result.attachment_errors.append(msg)
            audit.record(SYSTEM_USER, "ingest_fail", f"{space}/{path.name}#{name}", msg)
