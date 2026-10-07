"""PNG/JPEG files: the text comes only from the configured OCR engine (nothing else can read an image).

Fails (instead of creating an empty article) when OCR is not configured, the bytes are not really an image, or OCR
finds no text. OCR text may be misread; like the PDF parser, the parsed metadata notes it (the raw file itself only
keeps source/collected/published, so keep the original image next to it when accuracy matters).
"""
from __future__ import annotations

from pathlib import PurePath

from llmwiki.ingest.limits import check_size
from llmwiki.ingest.pdf import OcrEngine
from llmwiki.models import ParsedDocument

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def is_image(data: bytes) -> bool:
    return data.startswith(PNG) or data.startswith(JPEG)


def parse_image(data: bytes, source_name: str, ocr: OcrEngine | None = None, max_bytes: int | None = None) -> ParsedDocument:
    check_size(data, source_name, max_bytes)
    if not is_image(data):
        raise ValueError(f"not a valid image file: {source_name}")
    if ocr is None:
        raise ValueError(f"no extractable text: OCR is not configured for image {source_name}")
    text = ocr.ocr_page(data, 0).strip()  # the engine recognises image bytes (PNG/JPEG) and OCRs them directly
    if not text:
        raise ValueError(f"no extractable text: OCR found no text in {source_name}")
    return ParsedDocument(title=PurePath(source_name).stem, text=text, source_name=source_name,
                          metadata={"ocr": "image"})
