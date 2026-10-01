"""PDF parsing with pluggable text-layer extraction and OCR. No hard dependency on heavy libs."""
from __future__ import annotations

from pathlib import PurePath
from typing import Protocol, runtime_checkable

from llmwiki.models import ParsedDocument


@runtime_checkable
class PdfExtractor(Protocol):
    """Returns the text-layer text of each page ('' when a page has none)."""

    def extract_pages(self, data: bytes) -> list[str]: ...


@runtime_checkable
class OcrEngine(Protocol):
    """OCR for one page (0-based index) of the given PDF bytes. Engine is chosen later."""

    def ocr_page(self, data: bytes, page_index: int) -> str: ...


class PypdfExtractor:
    """Text-layer extractor backed by the optional `pypdf` package."""

    def __init__(self) -> None:
        try:
            import pypdf  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("pypdf is not installed; pass a PdfExtractor or install pypdf") from exc

    def extract_pages(self, data: bytes) -> list[str]:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        return [(page.extract_text() or "") for page in reader.pages]


class FakeOcrEngine:
    """Test double: returns canned text per page index and records the pages it was asked for."""

    def __init__(self, pages: dict[int, str] | None = None, default: str = "") -> None:
        self.pages = pages or {}
        self.default = default
        self.calls: list[int] = []

    def ocr_page(self, data: bytes, page_index: int) -> str:
        self.calls.append(page_index)
        return self.pages.get(page_index, self.default)


def parse_pdf(
    data: bytes,
    source_name: str,
    extractor: PdfExtractor | None = None,
    ocr: OcrEngine | None = None,
) -> ParsedDocument:
    """Text layer first; pages with no text go to `ocr` if given. metadata records OCR use."""
    extractor = extractor or PypdfExtractor()
    pages = extractor.extract_pages(data)
    out: list[str] = []
    ocr_pages: list[int] = []
    empty_pages: list[int] = []
    for i, page_text in enumerate(pages):
        if page_text.strip():
            out.append(page_text.strip())
        elif ocr is not None:
            ocr_pages.append(i + 1)
            out.append(ocr.ocr_page(data, i).strip())
        else:
            empty_pages.append(i + 1)
    metadata: dict[str, str] = {"pages": str(len(pages))}
    if ocr_pages:
        metadata["ocr_pages"] = ",".join(map(str, ocr_pages))  # 1-based; OCR text may be misread
    if empty_pages:
        metadata["empty_pages"] = ",".join(map(str, empty_pages))
    text = "\n\n".join(t for t in out if t)
    return ParsedDocument(title=PurePath(source_name).stem, text=text, source_name=source_name, metadata=metadata)
