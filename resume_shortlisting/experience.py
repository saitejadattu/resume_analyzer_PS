"""Structured extraction of the resume's Experience section (rule-based).

Turns the free-text Experience block into one :class:`ExperienceEntry` per role
— company, title, dates, duration, and the technologies named *inside that
entry* — plus a de-overlapped total.

Two deliberate constraints:

* **No inference.** Technologies come from :meth:`SkillsKB.detect_skills`, which
  matches explicit surface forms in that entry's own text. Phrase inference
  (``infer_from_phrases``) is *not* used here, and the Skills and Projects
  sections are never consulted, so a technology can only be attributed to a job
  the candidate actually named in it.
* **No guessing at dates.** An entry whose dates cannot be parsed is still
  returned, with ``duration_months`` left as ``None``; it simply contributes
  nothing to the total rather than being estimated.

Informational only: nothing here feeds the score or the recommendation.
"""

from __future__ import annotations

import re
from datetime import date

from .models import ExperienceEntry
from .skills_kb import SkillsKB, load_kb
from .utils import get_logger

logger = get_logger("experience")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_NAME = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# Order matters: "Jan 2024" and "06/2023" must be tried before a bare year.
_DATE = (
    rf"(?:{_MONTH_NAME}\s*['\-/,]?\s*(?:(?:19|20)\d{{2}}|'\d{{2}}|\d{{2}}(?!\d))"
    r"|\d{1,2}\s*[/\-]\s*(?:19|20)\d{2}"
    r"|(?:19|20)\d{2})"
)
_PRESENT = r"(?:present|current(?:ly)?|now|ongoing|till\s+date|to\s+date|till\s+now)"
_SEPARATOR = r"(?:\s*[–—]\s*|\s*-\s*|\s+to\s+|\s+until\s+|\s+through\s+)"

DATE_RANGE_RE = re.compile(
    rf"({_DATE}){_SEPARATOR}({_DATE}|{_PRESENT})", re.IGNORECASE
)

# En/em dashes lead bullets in many templates, so they belong here even though
# they also separate dates. Only a line-leading dash counts as a bullet.
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣–—]|\d+[.)])\s+")

# How many stacked header lines above a date line may belong to one entry.
_MAX_HEADER_LINES = 4

# A PDF flattens two columns into a stack, so the line above the dates is often
# the work location rather than the employer.
_LOCATION_WORDS = frozenset({
    "remote", "onsite", "on-site", "hybrid", "work from home", "wfh",
    "india", "usa", "uk", "bengaluru", "bangalore", "hyderabad", "chennai",
    "mumbai", "delhi", "new delhi", "noida", "gurugram", "gurgaon", "pune",
    "kolkata", "ahmedabad", "jaipur", "kochi", "indore", "chandigarh",
    "coimbatore", "bhubaneswar", "vizag", "visakhapatnam", "trivandrum",
})

# Words that identify which half of a header line is the job title.
_ROLE_HINTS = (
    "engineer", "developer", "intern", "analyst", "manager", "consultant",
    "scientist", "designer", "architect", "lead", "specialist", "trainee",
    "administrator", "associate", "executive", "officer", "researcher",
    "programmer", "sde", "freelance", "apprentice", "fellow", "head of",
    "devops", "mentee", "volunteer", "assistant", "coordinator", "tester",
)
# ...and which half is the employer.
_COMPANY_HINTS = (
    "technologies", "technology", "solutions", "systems", "software", "labs",
    "pvt", "private", "ltd", "limited", "inc", "llc", "llp", "corporation",
    "corp", "company", "services", "consulting", "global", "institute",
    "university", "college", "foundation", "studio", "media", "digital",
    "analytics", "networks", "group", "industries", "infotech", "bank",
)

_HEADER_SPLIT_RE = re.compile(r"\s*(?:[,|@·•/]|\bat\b|[–—]|\s-\s)\s*", re.IGNORECASE)


def _year(token: str) -> int:
    """Expand a 2-digit year; pass a 4-digit one through."""
    token = token.strip().lstrip("'")
    value = int(token)
    return value if value > 100 else 2000 + value


def parse_date(token: str, *, is_end: bool = False) -> tuple[int, int] | None:
    """Parse one resume date into ``(year, month)``.

    A year-only date resolves to January when it starts a range and December
    when it ends one, which is how "2023 - 2025" reads on a resume. Returns
    ``None`` when nothing usable is present.
    """
    token = token.strip().strip(".,;")
    if not token:
        return None
    if re.fullmatch(_PRESENT, token, re.IGNORECASE):
        today = date.today()
        return today.year, today.month

    name = re.match(rf"({_MONTH_NAME})\s*['\-/,]?\s*((?:19|20)\d{{2}}|'?\d{{2}})", token, re.IGNORECASE)
    if name:
        month = _MONTHS.get(name.group(1)[:3].lower())
        if month:
            return _year(name.group(2)), month

    numeric = re.match(r"(\d{1,2})\s*[/\-]\s*((?:19|20)\d{2})", token)
    if numeric:
        month = int(numeric.group(1))
        if 1 <= month <= 12:
            return int(numeric.group(2)), month

    bare = re.fullmatch(r"(19|20)\d{2}", token)
    if bare:
        return int(token), (12 if is_end else 1)
    return None


def _month_index(point: tuple[int, int]) -> int:
    """Months since year 0, so ranges can be compared as plain integers."""
    year, month = point
    return year * 12 + (month - 1)


def format_duration(months: int | None) -> str:
    """Render a month count the way a recruiter reads it."""
    if months is None or months <= 0:
        return "Not available"
    years, remainder = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if remainder:
        parts.append(f"{remainder} month{'s' if remainder != 1 else ''}")
    return " ".join(parts) or "Not available"


def _split_entries(section: str) -> list[str]:
    """Break the Experience section into one block per role.

    A line carrying a date range anchors an entry, and every non-bullet line
    directly above it joins that entry: a PDF flattens a two-column layout into
    a stack, so company, role and location each land on their own line. With no
    dates anywhere the whole section is kept as one entry rather than discarded.
    """
    lines = section.split("\n")
    anchors = [i for i, line in enumerate(lines) if DATE_RANGE_RE.search(line)]
    if not anchors:
        stripped = section.strip()
        return [stripped] if stripped else []

    starts: list[int] = []
    for position, anchor in enumerate(anchors):
        # Never walk back past the previous entry's own date line.
        floor = anchors[position - 1] + 1 if position else 0
        start = anchor
        index = anchor - 1
        while index >= max(floor, anchor - _MAX_HEADER_LINES):
            line = lines[index]
            # A bullet or a blank line ends the header block above.
            if not line.strip() or _BULLET_RE.match(line):
                break
            start = index
            index -= 1
        if starts and start <= starts[-1]:
            start = anchor
        if starts and start <= starts[-1]:
            continue
        starts.append(start)

    blocks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end]).strip()
        if block:
            blocks.append(block)
    return blocks


def _header_parts(block: str) -> list[str]:
    """Header fragments of a block, in reading order, with dates stripped out.

    Everything above the first bullet is header. A PDF stacks a two-column
    entry as company / role / location / dates, so each of those arrives on its
    own line and all of them have to be considered — reading only the line
    directly above the dates picks up the location instead of the employer.
    """
    parts: list[str] = []
    for line in block.split("\n"):
        if _BULLET_RE.match(line):
            break
        cleaned = DATE_RANGE_RE.sub(" ", line)
        cleaned = re.sub(r"\((?:[^)]*)\)", " ", cleaned)
        for piece in _HEADER_SPLIT_RE.split(cleaned):
            piece = piece.strip(" .,:;|-–—")
            if piece and not piece.isdigit() and _looks_like_a_name(piece):
                parts.append(piece)
    return parts


def _looks_like_a_name(part: str) -> bool:
    """True if a fragment could be a company or job title, not prose.

    A wrapped bullet loses its marker when a PDF is flattened, so its
    continuation line can look like a header. Names are short, title-cased and
    do not end a sentence; prose usually fails at least one of those.
    """
    words = part.split()
    if not words or len(words) > 8 or len(part) > 60:
        return False
    if part.rstrip().endswith("."):
        return False
    # A multi-word fragment opening in lower case is a continuation, not a name.
    return not (part[0].islower() and len(words) > 3)


def _is_location(part: str) -> bool:
    return part.strip().casefold() in _LOCATION_WORDS


def _company_and_role(block: str) -> tuple[str, str]:
    """Best-effort company/role from a block's header lines.

    Returns empty strings for anything that cannot be identified — an unlabelled
    fragment is left blank rather than guessed at.
    """
    parts = _header_parts(block)
    if not parts:
        return "", ""

    def has(part: str, hints) -> bool:
        lowered = part.lower()
        return any(hint in lowered for hint in hints)

    # Drop a trailing location so it is never mistaken for the employer.
    while len(parts) > 1 and (
        _is_location(parts[-1])
        or (len(parts) >= 3 and not has(parts[-1], _ROLE_HINTS) and not has(parts[-1], _COMPANY_HINTS))
    ):
        parts.pop()

    role = next((p for p in parts if has(p, _ROLE_HINTS)), "")
    company = next((p for p in parts if p != role and has(p, _COMPANY_HINTS)), "")
    if not company:
        # Employers rarely announce themselves with a keyword, so fall back to
        # the first fragment that is not the role.
        remaining = [p for p in parts if p != role and not _is_location(p)]
        company = remaining[0] if remaining else ""
    # No fallback for the role: an unlabelled fragment is left blank rather
    # than promoted to a job title it may not be.
    return company, role


def explicit_technologies(text: str, kb: SkillsKB) -> list[str]:
    """Technologies this text names outright, in first-seen order.

    A hit that sits entirely inside a longer technology's match is discarded:
    the KB aliases "js" to JavaScript, which would otherwise make every mention
    of "Node.js" silently claim JavaScript too. Only genuinely separate
    mentions survive, so nothing is attributed that the entry does not say.
    """
    spans = kb.detect_skill_spans(text)
    ordered: list[tuple[str, int]] = []
    for canonical, start, end in spans:
        swallowed = any(
            other != canonical and other_start <= start and end <= other_end
            and (other_end - other_start) > (end - start)
            for other, other_start, other_end in spans
        )
        if not swallowed:
            ordered.append((canonical, start))
    seen: set[str] = set()
    result: list[str] = []
    for canonical, _ in sorted(ordered, key=lambda item: item[1]):
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def extract_entries(experience_text: str, kb: SkillsKB | None = None) -> list[ExperienceEntry]:
    """Extract one :class:`ExperienceEntry` per role from the Experience text."""
    if not experience_text or not experience_text.strip():
        return []
    kb = kb or load_kb()

    entries: list[ExperienceEntry] = []
    for block in _split_entries(experience_text):
        company, role = _company_and_role(block)

        start_label = end_label = ""
        months: int | None = None
        match = DATE_RANGE_RE.search(block)
        if match:
            start_label, end_label = match.group(1).strip(), match.group(2).strip()
            start = parse_date(start_label)
            end = parse_date(end_label, is_end=True)
            if start and end:
                span = _month_index(end) - _month_index(start) + 1
                # A reversed or absurd range is unreliable, so report nothing.
                months = span if 0 < span <= 720 else None

        entries.append(
            ExperienceEntry(
                company=company,
                role=role,
                start_date=start_label,
                end_date=end_label,
                duration_months=months,
                duration=format_duration(months),
                # Explicit mentions inside THIS entry only — no inference, and
                # never sourced from the Skills or Projects sections.
                technologies=explicit_technologies(block, kb),
                text=block,
            )
        )
    return entries


def total_experience_months(entries: list[ExperienceEntry]) -> int | None:
    """Total months worked, merging overlapping roles so they count once.

    Returns ``None`` when no entry carried usable dates.
    """
    spans: list[tuple[int, int]] = []
    for entry in entries:
        if entry.duration_months is None or not entry.start_date:
            continue
        start = parse_date(entry.start_date)
        end = parse_date(entry.end_date, is_end=True)
        if start and end:
            spans.append((_month_index(start), _month_index(end)))
    if not spans:
        return None

    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        # Adjacent months (Jan-Mar then Apr-Jun) form one continuous stretch.
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start + 1 for start, end in merged)


def extract_experience(
    experience_text: str, kb: SkillsKB | None = None
) -> tuple[list[ExperienceEntry], int | None]:
    """Convenience wrapper returning ``(entries, total_months)``."""
    entries = extract_entries(experience_text, kb)
    return entries, total_experience_months(entries)


# --------------------------------------------------------------------------- #
# Results-table cells
# --------------------------------------------------------------------------- #
#: Columns appended to the full results table, in order.
TABLE_COLUMNS: list[str] = ["Total Experience", "Company Name", "Role", "Tech Stack"]

NOT_MENTIONED = "Not Mentioned"


def table_fields(
    entries: list[ExperienceEntry], total_experience: str = ""
) -> dict[str, str]:
    """Per-candidate results-table cells for the experience columns.

    Every entry is kept: the company, role and tech-stack cells carry one line
    per experience, in the same order, so line *n* of each column describes the
    same job. A field the resume never stated reads "Not Mentioned" rather than
    borrowing from the Skills or Projects sections.
    """
    if not entries:
        return {column: "" for column in TABLE_COLUMNS}
    return {
        "Total Experience": total_experience or "Not available",
        "Company Name": "\n".join(entry.company or NOT_MENTIONED for entry in entries),
        "Role": "\n".join(entry.role or NOT_MENTIONED for entry in entries),
        "Tech Stack": "\n".join(
            ", ".join(entry.technologies) or NOT_MENTIONED for entry in entries
        ),
    }
