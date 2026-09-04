"""PDF -> clean text extraction using PyMuPDF (Step 3).

Extracted text is cached under ``/extracted_text`` as ``.txt`` so repeated runs
never re-parse the same PDF (Step 21).
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF
from docx import Document

from .models import Candidate
from .utils import (
    clean_text,
    extracted_text_path,
    get_logger,
    safe_read_text,
    safe_write_text,
)

logger = get_logger("text_extractor")


class TextExtractionError(Exception):
    """Raised when a PDF cannot be read at all."""


def _links_from_pdf(pdf_path: Path) -> list[str]:
    """Return every external URI attached to the PDF as a link annotation."""
    urls: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            for link in page.get_links():
                uri = link.get("uri")
                if uri:
                    urls.append(uri)
    return urls


def _links_from_docx(path: Path) -> list[str]:
    """Return every external hyperlink target in a .docx."""
    return [
        rel.target_ref
        for rel in Document(path).part.rels.values()
        if rel.reltype.endswith("/hyperlink") and rel.is_external
    ]


def extract_link_urls(path: Path) -> list[str]:
    """Recover hyperlink targets that are not present in the visible text.

    Resume templates commonly render a GitHub/LeetCode link as an icon plus a
    bare handle, leaving the real URL only in the file's link annotations. This
    never raises: a file we cannot read simply contributes no links.
    """
    path = Path(path)
    try:
        urls = _links_from_docx(path) if path.suffix.lower() == ".docx" else _links_from_pdf(path)
    except Exception as exc:  # noqa: BLE001 - link recovery is best-effort
        logger.debug("No link annotations read from %s: %s", path, exc)
        return []
    # Preserve order, drop duplicates and anything that is not a web link.
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        key = url.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            ordered.append(url)
    return ordered


def _extract_from_pdf(pdf_path: Path) -> str:
    """Read every page of a PDF and return concatenated text."""
    parts: list[str] = []
    # ``fitz.open`` raises fitz.FileDataError on corrupt files.
    with fitz.open(pdf_path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts)


def _extract_from_docx(path: Path) -> str:
    return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)


def extract_text(
    candidate: Candidate,
    pdf_path: Path,
    *,
    use_cache: bool = True,
) -> str:
    """Extract clean text from a resume PDF, using the on-disk cache (Step 3).

    Returns cleaned text, or an empty string if extraction fails (the caller
    records a remark and continues — one bad PDF never stops the run).
    """
    cache_path = extracted_text_path(candidate.resume_url, candidate.name)

    if use_cache and cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info(
            "[%s] Using cached extracted text: %s",
            candidate.display_name,
            cache_path.name,
        )
        return safe_read_text(cache_path)

    try:
        raw = _extract_from_docx(pdf_path) if pdf_path.suffix.lower() == ".docx" else _extract_from_pdf(pdf_path)
    except Exception as exc:  # noqa: BLE001 - fitz raises several error types
        logger.warning(
            "[%s] Failed to extract text from %s: %s",
            candidate.display_name,
            pdf_path.name,
            exc,
        )
        return ""

    cleaned = clean_text(raw)
    if not cleaned:
        logger.warning(
            "[%s] Extracted text is empty (scanned/image PDF?): %s",
            candidate.display_name,
            pdf_path.name,
        )
        return ""

    if use_cache:
        safe_write_text(cache_path, cleaned)

    logger.info(
        "[%s] Extracted %d chars of text", candidate.display_name, len(cleaned)
    )
    return cleaned
