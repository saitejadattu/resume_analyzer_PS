"""Resume downloading with retries, caching, and robust error handling.

Steps 2 & 21:
    * Download each resume via ``requests`` into ``/resumes``.
    * Skip download if already cached.
    * Retry with exponential backoff; handle timeout / 404 / invalid URL /
      network failure without ever aborting the whole run.
    * Concurrent downloads via ThreadPoolExecutor.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re

import requests
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import config
from .models import Candidate
from .utils import get_logger, is_valid_url, resume_cache_path

logger = get_logger("downloader")

_GOOGLE_DRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([^/?#]+)", re.IGNORECASE)
# Students often share the folder holding the resume rather than the file.
_GOOGLE_FOLDER_RE = re.compile(
    r"drive\.google\.com/drive/(?:u/\d+/)?folders/([0-9A-Za-z_-]+)", re.IGNORECASE
)
# ...or a Google Doc, whose page is HTML until it is exported.
_GOOGLE_DOC_RE = re.compile(
    r"docs\.google\.com/(document|presentation)/d/([0-9A-Za-z_-]+)", re.IGNORECASE
)
# Item ids on a rendered Drive folder page.
_FOLDER_ITEM_RE = re.compile(r'data-id="([0-9A-Za-z_-]{20,})"')
# Only the first few items are considered, so one odd folder cannot stall a run.
_MAX_FOLDER_ITEMS = 5

# Payloads that are definitely not a resume, whatever the content-type claims.
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n", b"GIF8", b"BM", b"RIFF")


def _google_drive_file_id(url: str) -> str | None:
    """Extract a Drive file ID from the supported public URL forms."""
    match = _GOOGLE_DRIVE_FILE_RE.search(url)
    if match:
        return match.group(1)

    parsed = urlparse(url)
    if parsed.netloc.casefold() not in {"drive.google.com", "www.drive.google.com"}:
        return None
    if parsed.path.casefold() not in {"/open", "/uc"}:
        return None
    return parse_qs(parsed.query).get("id", [None])[0]

# Transient errors worth retrying (timeouts, connection resets). HTTP 4xx like
# 404 are NOT retried — they are permanent and raised as PermanentDownloadError.
_RETRYABLE = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class DownloadError(Exception):
    """Base class for download failures."""


class PermanentDownloadError(DownloadError):
    """A non-retryable failure (invalid URL, 404, not a PDF, too large)."""


@dataclass
class DownloadResult:
    """Outcome of attempting to download one resume."""

    candidate: Candidate
    path: Path | None
    ok: bool
    cached: bool = False
    error: str = ""


def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    """Heuristic: is this payload actually a PDF?

    The magic bytes win. A content-type alone is not enough — Drive labels a
    photo named ``resume.pdf`` as ``application/pdf``, and storing that as a
    resume only produces an empty extraction later.
    """
    head = content[:1024]
    if head[:5] == b"%PDF-" or b"%PDF-" in head:
        return True
    if head.startswith(_IMAGE_MAGIC) or b"<html" in head[:512].lower():
        return False
    return "application/pdf" in content_type.lower()


def _resume_suffix(url: str, content_type: str = "") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".pdf", ".docx", ".doc"}:
        return suffix
    if "wordprocessingml" in content_type.lower():
        return ".docx"
    if "msword" in content_type.lower():
        return ".doc"
    return ".pdf"


def _looks_like_resume(content: bytes, content_type: str, suffix: str) -> bool:
    if suffix == ".pdf": return _looks_like_pdf(content, content_type)
    if suffix == ".docx": return content[:2] == b"PK" or "wordprocessingml" in content_type.lower()
    if suffix == ".doc": return content[:8] == bytes.fromhex("D0CF11E0A1B11AE1") or "msword" in content_type.lower()
    return False


def _drive_download_url(file_id: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def _resolve_drive_folder(url: str, timeout: int) -> list[str]:
    """Return direct-download URLs for the files inside a public Drive folder.

    The folder page is HTML, so a folder link downloads nothing useful on its
    own. Item ids are read straight off that page — no API key needed. Returns
    an empty list when the folder is private or empty, and never raises.
    """
    folder_id = _google_drive_folder_id(url)
    if not folder_id:
        return []
    try:
        resp = requests.get(
            url, timeout=timeout, headers={"User-Agent": config.USER_AGENT},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - listing is best-effort
        logger.info("Could not list Drive folder %s: %s", folder_id, exc)
        return []

    ordered: list[str] = []
    for item_id in _FOLDER_ITEM_RE.findall(resp.text):
        if item_id != folder_id and item_id not in ordered:
            ordered.append(item_id)
    logger.info("Drive folder %s exposed %d item(s)", folder_id, len(ordered))
    return [_drive_download_url(i) for i in ordered[:_MAX_FOLDER_ITEMS]]


def _google_drive_folder_id(url: str) -> str | None:
    match = _GOOGLE_FOLDER_RE.search(url)
    return match.group(1) if match else None


def _candidate_urls(url: str, timeout: int) -> list[str]:
    """Every URL worth trying for one sheet entry, best candidate first."""
    if _google_drive_folder_id(url):
        # Empty list here means "folder unreadable or empty" — reported as
        # such, rather than falling back to downloading the HTML folder page.
        return _resolve_drive_folder(url, timeout)

    doc = _GOOGLE_DOC_RE.search(url)
    if doc:
        # A Doc/Slides page is HTML; its PDF export is a real resume.
        kind, doc_id = doc.group(1), doc.group(2)
        return [f"https://docs.google.com/{kind}/d/{doc_id}/export?format=pdf"]

    file_id = _google_drive_file_id(url)
    if file_id:
        return [_drive_download_url(file_id)]
    return [url]


def _download_once(url: str, timeout: int) -> requests.Response:
    """Single HTTP GET; raises for status so 404 propagates as HTTPError."""
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": config.USER_AGENT},
        stream=True,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp


def download_resume(candidate: Candidate, settings: config.Settings) -> DownloadResult:
    """Download a single resume with validation, caching, and retries."""
    url = (candidate.resume_url or "").strip()

    if not is_valid_url(url):
        msg = f"Invalid or empty resume URL: {url!r}"
        logger.warning("[%s] %s", candidate.display_name, msg)
        return DownloadResult(candidate, None, ok=False, error=msg)

    suffix = _resume_suffix(url)
    dest = resume_cache_path(url, candidate.name, suffix)

    # Step 21: cache hit — skip re-download.
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("[%s] Using cached resume: %s", candidate.display_name, dest.name)
        return DownloadResult(candidate, dest, ok=True, cached=True)

    # Build a retrying downloader bound to this run's settings.
    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(settings.download_retries),
        wait=wait_exponential(multiplier=settings.download_backoff, min=1, max=20),
        reraise=True,
    )
    def _attempt(target: str) -> bytes:
        resp = _download_once(target, settings.download_timeout)
        # Enforce a max size while streaming to avoid memory blow-ups.
        chunks = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            chunks.extend(chunk)
            if len(chunks) > config.MAX_RESUME_BYTES:
                raise PermanentDownloadError(
                    f"Resume exceeds max size ({config.MAX_RESUME_BYTES} bytes)"
                )
        content_type = resp.headers.get("Content-Type", "")
        detected_suffix = _resume_suffix(url, content_type)
        if not _looks_like_resume(bytes(chunks), content_type, detected_suffix):
            raise PermanentDownloadError(
                f"Downloaded content is not a supported PDF/DOCX/DOC resume (content-type={content_type!r})"
            )
        return bytes(chunks), detected_suffix

    try:
        targets = _candidate_urls(url, settings.download_timeout)
        if not targets:
            msg = "Shared Drive folder is empty or not publicly accessible"
            logger.warning("[%s] %s: %s", candidate.display_name, msg, url)
            return DownloadResult(candidate, None, ok=False, error=msg)
        last_error: Exception | None = None
        content = detected_suffix = None
        for target in targets:
            try:
                content, detected_suffix = _attempt(target)
                break
            except (PermanentDownloadError, requests.HTTPError) as exc:
                # A folder can hold non-resume files; keep trying the rest.
                last_error = exc
                content = None
        if content is None:
            raise last_error if last_error else PermanentDownloadError("no downloadable resume found")
        if detected_suffix != suffix:
            dest = resume_cache_path(url, candidate.name, detected_suffix)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detail = {
            403: "file inaccessible or permission denied",
            404: "file not found",
            429: "rate limited",
        }.get(status, "downloading resume")
        msg = f"HTTP {status}: {detail}"
        logger.warning("[%s] %s: %s", candidate.display_name, msg, url)
        return DownloadResult(candidate, None, ok=False, error=msg)
    except PermanentDownloadError as exc:
        logger.warning("[%s] %s", candidate.display_name, exc)
        return DownloadResult(candidate, None, ok=False, error=str(exc))
    except (RetryError, *_RETRYABLE) as exc:
        msg = f"Network failure after retries: {type(exc).__name__}"
        logger.warning("[%s] %s: %s", candidate.display_name, msg, url)
        return DownloadResult(candidate, None, ok=False, error=msg)
    except Exception as exc:  # noqa: BLE001 - never let one resume kill the run
        msg = f"Unexpected download error: {type(exc).__name__}: {exc}"
        logger.error("[%s] %s", candidate.display_name, msg)
        return DownloadResult(candidate, None, ok=False, error=msg)

    # Persist to cache.
    try:
        dest.write_bytes(content)
    except OSError as exc:
        msg = f"Could not save resume to disk: {exc}"
        logger.error("[%s] %s", candidate.display_name, msg)
        return DownloadResult(candidate, None, ok=False, error=msg)

    logger.info(
        "[%s] Downloaded resume (%d KB) -> %s",
        candidate.display_name,
        len(content) // 1024,
        dest.name,
    )
    return DownloadResult(candidate, dest, ok=True)


def download_all(
    candidates: list[Candidate],
    settings: config.Settings,
    on_progress=None,
) -> dict[str, DownloadResult]:
    """Download every resume concurrently (Steps 2, 21).

    Returns a mapping keyed by resume URL so callers can look up results.
    Continues past individual failures. ``on_progress(done, total)`` is called
    after each download completes (used by the UI progress bar).
    """
    results: dict[str, DownloadResult] = {}
    if not candidates:
        return results

    workers = max(1, min(settings.download_workers, len(candidates)))
    total = len(candidates)
    logger.info("Downloading %d resume(s) with %d worker(s)", total, workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_resume, cand, settings): cand
            for cand in candidates
        }
        done = 0
        for future in as_completed(futures):
            cand = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive
                logger.error("[%s] Worker crashed: %s", cand.display_name, exc)
                result = DownloadResult(
                    cand, None, ok=False, error=f"Worker crash: {exc}"
                )
            results[cand.resume_url] = result
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    ok = sum(1 for r in results.values() if r.ok)
    logger.info("Download summary: %d ok, %d failed", ok, len(results) - ok)
    return results
