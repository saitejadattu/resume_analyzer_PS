"""Shared utilities: logging, filename hashing, safe IO, text cleaning.

Kept dependency-light and side-effect-free (aside from ``setup_logging``) so
every module can import from here without circular imports.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from . import config

_LOGGER_NAME = "resume_shortlisting"
_logging_configured = False


def setup_logging(level: int = logging.INFO, *, to_console: bool = True) -> logging.Logger:
    """Configure and return the package logger (Step 18).

    Logs to ``logs/app.log`` and, optionally, the console. Idempotent: calling
    it multiple times will not attach duplicate handlers.
    """
    global _logging_configured
    logger = logging.getLogger(_LOGGER_NAME)

    if _logging_configured:
        return logger

    config.ensure_dirs()
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)

    _logging_configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the package logger."""
    base = logging.getLogger(_LOGGER_NAME)
    return base.getChild(name) if name else base


# --------------------------------------------------------------------------- #
# Filenames / hashing
# --------------------------------------------------------------------------- #
def url_hash(url: str) -> str:
    """Stable short hash of a URL, used as a cache key (Step 21)."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(value: str, *, max_len: int = 60) -> str:
    """Turn arbitrary text into a filesystem-safe slug."""
    cleaned = _SAFE_CHARS.sub("_", value.strip()).strip("_")
    cleaned = cleaned or "file"
    return cleaned[:max_len]


def resume_cache_path(url: str, name: str = "", suffix: str = ".pdf") -> Path:
    """Deterministic managed cache path for a downloaded resume."""
    prefix = safe_filename(name, max_len=40) + "_" if name else ""
    suffix = suffix.lower() if suffix.lower() in {".pdf", ".docx", ".doc"} else ".pdf"
    return config.RESUMES_DIR / f"{prefix}{url_hash(url)}{suffix}"


def extracted_text_path(url: str, name: str = "") -> Path:
    """Deterministic path for extracted resume text (Step 21 cache)."""
    prefix = safe_filename(name, max_len=40) + "_" if name else ""
    return config.EXTRACTED_TEXT_DIR / f"{prefix}{url_hash(url)}.txt"


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def is_valid_url(url: str) -> bool:
    """Basic structural validation of an http(s) URL (Step 2)."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# --------------------------------------------------------------------------- #
# Text cleaning
# --------------------------------------------------------------------------- #
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Normalise extracted PDF text: collapse runs of spaces / blank lines."""
    if not text:
        return ""
    # Normalise line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse horizontal whitespace but preserve newlines (layout matters for
    # section detection).
    lines = [_WHITESPACE_RUN.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def normalize_token(token: str) -> str:
    """Lower-case and trim a skill/tech token for comparison."""
    return token.strip().lower()


def safe_read_text(path: Path) -> str:
    """Read a text file, returning '' on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def safe_write_text(path: Path, content: str) -> bool:
    """Write text to a file, returning success as a bool."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False
