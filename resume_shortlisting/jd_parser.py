"""Job Description parsing (Step 5).

Accepts a JD as ``.txt``, ``.pdf``, ``.docx``, or a raw string, and produces a
:class:`JDSpec` of ``required`` and ``preferred`` skills.

Strategy:
    * Read the raw text (format-aware).
    * Split into a "required" region and a "preferred" region using common
      heading cues ("required", "must have" vs "preferred", "nice to have").
    * Within each region, detect known skills via the skills KB, and also pull
      explicit comma/bullet-listed tokens so unusual skills are not lost.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF
import docx  # python-docx

from .models import JDSpec
from .skills_kb import SkillsKB, load_kb
from .utils import clean_text, get_logger

logger = get_logger("jd_parser")

# Headings that introduce the "required" vs "preferred" buckets.
_REQUIRED_CUES = re.compile(
    r"(required|must[\s-]?have|essential|mandatory|minimum qualifications?|"
    r"key skills|core skills)",
    re.IGNORECASE,
)
_PREFERRED_CUES = re.compile(
    r"(preferred|nice[\s-]?to[\s-]?have|good[\s-]?to[\s-]?have|bonus|"
    r"desirable|plus|added advantage)",
    re.IGNORECASE,
)

# Split a listing line into individual tokens.
_LIST_SPLIT = re.compile(r"[,;/|\n•·\-–]|\band\b|\bor\b", re.IGNORECASE)

# Tokens that are headings/boilerplate, not skills — filtered out of results.
_STOPWORD_TOKENS = re.compile(
    r"^(?:.*\b(?:skills?|qualifications?|requirements?|responsibilit|"
    r"experience|description|preferred|required|must|nice|essential|"
    r"mandatory|desirable|bonus|proficiency|knowledge of|familiarity|"
    r"understanding|ability|strong|good|excellent|years?)\b.*)$",
    re.IGNORECASE,
)


def _is_stopword_token(token: str) -> bool:
    """True if a token is heading/boilerplate text rather than a real skill."""
    return bool(_STOPWORD_TOKENS.match(token.strip()))


def _read_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_pdf(path: Path) -> str:
    parts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts)


def _read_docx(path: Path) -> str:
    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


def read_jd_text(source: str | Path) -> str:
    """Read JD text from a file path or return the string itself if not a file."""
    # Allow passing the JD inline as a plain string.
    candidate_path = Path(str(source))
    if not candidate_path.exists():
        logger.info("Treating --jd argument as inline JD text")
        return clean_text(str(source))

    suffix = candidate_path.suffix.lower()
    logger.info("Reading JD from %s", candidate_path)
    if suffix == ".pdf":
        raw = _read_pdf(candidate_path)
    elif suffix in (".docx", ".doc"):
        raw = _read_docx(candidate_path)
    else:  # .txt / .md / anything else -> plain text
        raw = _read_txt(candidate_path)
    return clean_text(raw)


def _extract_listed_tokens(text: str, kb: SkillsKB) -> list[str]:
    """Pull skill tokens from a region: KB skills + explicit list items."""
    found: list[str] = []

    # 1. Known skills detected anywhere in the region (canonical names).
    found.extend(kb.detect_skills(text))

    # 2. Explicit list items (captures skills the KB doesn't know yet).
    for raw in _LIST_SPLIT.split(text):
        token = raw.strip(" .:\t()").strip()
        if not (2 <= len(token) <= 30):
            continue
        canonical = kb.canonical_of(token)
        if canonical is None and _is_stopword_token(token):
            # Heading/boilerplate text (e.g. "Required Skills") — not a skill.
            continue
        found.append(canonical if canonical else token)

    # De-duplicate case-insensitively, preserve order.
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in found:
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(tok)
    return ordered


def _split_regions(text: str) -> tuple[str, str]:
    """Split JD text into (required_region, preferred_region).

    If a "preferred" heading exists, everything from it onward is preferred and
    everything before it (from any required cue) is required. When no cues are
    present, the whole document is treated as required.
    """
    pref_match = _PREFERRED_CUES.search(text)
    if pref_match:
        required_region = text[: pref_match.start()]
        preferred_region = text[pref_match.start():]
    else:
        required_region = text
        preferred_region = ""

    # If there's an explicit required cue, prefer text after it for "required".
    req_match = _REQUIRED_CUES.search(required_region)
    if req_match:
        required_region = required_region[req_match.start():]

    return required_region, preferred_region


def parse_jd(source: str | Path, kb: SkillsKB | None = None) -> JDSpec:
    """Parse a JD into required + preferred skill lists (Step 5)."""
    kb = kb or load_kb()
    text = read_jd_text(source)
    if not text.strip():
        logger.warning("JD is empty after reading %r", source)
        return JDSpec()

    required_region, preferred_region = _split_regions(text)

    required = _extract_listed_tokens(required_region, kb)
    preferred = _extract_listed_tokens(preferred_region, kb)

    # A skill can't be both; required wins.
    required_lower = {s.lower() for s in required}
    preferred = [s for s in preferred if s.lower() not in required_lower]

    spec = JDSpec(required=required, preferred=preferred)
    logger.info(
        "Parsed JD: %d required, %d preferred skill(s)",
        len(spec.required),
        len(spec.preferred),
    )
    return spec
