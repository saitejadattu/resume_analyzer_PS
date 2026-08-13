"""Resume parsing: split text into sections and extract projects (Steps 4, 9, 11).

The parser is deliberately heuristic and dependency-free. Resumes are wildly
inconsistent, so we detect section headings by matching short lines against a
configurable synonym list, then slice the text between consecutive headings.
"""

from __future__ import annotations

import re

from . import config
from .models import GithubKind, ParsedResume, Project
from .github_checker import classify_kind
from .utils import get_logger

logger = get_logger("parser")

# Find any github.com URL (profile or repo), with or without scheme (Step 9).
GITHUB_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.\-/]+",
    re.IGNORECASE,
)

# Generic http(s) URL, used to spot "live"/demo links in a project block.
GENERIC_URL_RE = re.compile(r"https?://[^\s)\]}<>\"']+", re.IGNORECASE)

# A bullet/line lead-in used to split individual projects.
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣]|\d+[.)])\s+")

# Build a reverse lookup: lower-cased heading synonym -> canonical section.
_HEADING_LOOKUP: dict[str, str] = {
    syn.lower(): canonical
    for canonical, synonyms in config.SECTION_HEADINGS.items()
    for syn in synonyms
}
# Longest-first so "technical skills" wins over "skills".
_SORTED_HEADINGS: list[tuple[str, str]] = sorted(
    _HEADING_LOOKUP.items(), key=lambda kv: len(kv[0]), reverse=True
)


def _match_heading(line: str) -> str | None:
    """Return the canonical section if a line is a section heading, else None.

    Headings are short lines (few words) that start with a known synonym. This
    avoids misclassifying a sentence that merely contains the word "projects".
    """
    stripped = line.strip().strip(":").strip()
    if not stripped or len(stripped) > 40:
        return None

    lowered = stripped.lower()
    word_count = len(lowered.split())
    if word_count > 4:
        return None

    for synonym, canonical in _SORTED_HEADINGS:
        # Exact match, or heading followed by a separator (e.g. "SKILLS :").
        if lowered == synonym or lowered.startswith(synonym + " "):
            return canonical
    return None


def split_sections(text: str) -> dict[str, str]:
    """Split resume text into canonical sections (Step 4).

    Returns a dict with keys from ``config.SECTION_HEADINGS``. Missing sections
    map to an empty string.
    """
    sections: dict[str, list[str]] = {key: [] for key in config.SECTION_HEADINGS}
    current: str | None = None

    for line in text.split("\n"):
        heading = _match_heading(line)
        if heading is not None:
            current = heading
            # Keep any trailing text on the heading line (e.g. "Skills: Python").
            remainder = line.strip().strip(":")
            for syn, canon in _SORTED_HEADINGS:
                if canon == heading and remainder.lower().startswith(syn):
                    tail = remainder[len(syn):].strip(" :-")
                    if tail:
                        sections[heading].append(tail)
                    break
            continue
        if current is not None:
            sections[current].append(line)

    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _split_projects(projects_text: str) -> list[str]:
    """Break the Projects section into individual project blocks."""
    if not projects_text.strip():
        return []

    lines = projects_text.split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if not line.strip():
            # Blank line -> soft boundary only if we already have content.
            if current:
                current.append("")
            continue
        # A new bullet or a short Title-Case line starts a new project.
        # Technology/link labels belong to the current project, not a new one.
        is_project_field = re.match(r"^\s*(?:tech(?:nologies)?(?:\s+used)?|tech\s*stack|tools?|github|repository|repo|live\s*(?:url|demo)?)\s*[:\-]", line, re.I)
        if _BULLET_RE.match(line) or (_is_probable_title(line) and not is_project_field):
            if current:
                blocks.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        blocks.append(current)

    # Join, trim, and drop empties.
    result = ["\n".join(b).strip() for b in blocks]
    return [b for b in result if b]


def _is_probable_title(line: str) -> bool:
    """Heuristic: a short, capitalised, punctuation-light line reads as a title."""
    stripped = line.strip()
    if not (3 <= len(stripped) <= 80):
        return False
    words = stripped.split()
    if len(words) > 8:
        return False
    # Titles rarely end in a period and often are Title/UPPER case.
    if stripped.endswith("."):
        return False
    alpha_words = [w for w in words if w[:1].isalpha()]
    if not alpha_words:
        return False
    capitalised = sum(1 for w in alpha_words if w[0].isupper())
    return capitalised / len(alpha_words) >= 0.6


def _project_name(block: str) -> str:
    """First meaningful line of a project block is treated as its name."""
    for line in block.split("\n"):
        cleaned = _BULLET_RE.sub("", line).strip(" :-\t")
        if cleaned:
            # Trim an inline tech list after a dash/colon for a cleaner name.
            cleaned = re.split(r"\s[-–|:]\s", cleaned, maxsplit=1)[0].strip()
            return cleaned[:120]
    return ""


def build_projects(projects_text: str) -> list[Project]:
    """Extract structured Project objects from the Projects section (Step 11).

    Technologies are filled in later by :mod:`tech_detector`; here we capture
    name, description, and any GitHub / live links found in the block.
    """
    projects: list[Project] = []
    for block in _split_projects(projects_text):
        github = ""
        gh_match = GITHUB_URL_RE.search(block)
        if gh_match:
            github = _normalize_url(gh_match.group(0))

        live = ""
        for url in GENERIC_URL_RE.findall(block):
            if "github.com" not in url.lower():
                live = url.rstrip(".,);")
                break

        projects.append(
            Project(
                name=_project_name(block),
                description=block,
                github_url=github,
                live_url=live,
            )
        )
    return projects


def find_github_urls(text: str) -> list[str]:
    """Return all unique GitHub URLs in the resume text (Step 9)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in GITHUB_URL_RE.findall(text):
        url = _normalize_url(raw.rstrip(".,);"))
        key = url.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            ordered.append(url)
    return ordered


def _normalize_url(url: str) -> str:
    """Ensure a URL has an https scheme."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def parse_resume(text: str) -> ParsedResume:
    """Full parse: sections + projects + github links (Steps 4, 9, 11)."""
    if not text.strip():
        return ParsedResume()

    sections = split_sections(text)
    projects_text = sections.get("projects", "")

    parsed = ParsedResume(
        skills=sections.get("skills", ""),
        projects=projects_text,
        experience=sections.get("experience", ""),
        education=sections.get("education", ""),
        certifications=sections.get("certifications", ""),
        project_list=build_projects(projects_text),
        github_urls=find_github_urls(text),
        candidate_github_urls=[
            url for url in find_github_urls(text)
            if classify_kind(url)[0] is GithubKind.PROFILE
        ],
        raw_text=text,
    )

    logger.debug(
        "Parsed resume: %d project(s), %d github url(s)",
        len(parsed.project_list),
        len(parsed.github_urls),
    )
    return parsed
