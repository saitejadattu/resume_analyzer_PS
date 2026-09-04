"""Central configuration for the Resume Shortlisting System.

All tunable values live here so behaviour can be changed without touching the
business logic (Step 17). Values may be overridden via environment variables
where noted, which keeps secrets (e.g. an optional GitHub token) out of source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Folders (Step 16)
# --------------------------------------------------------------------------- #
# Package root: .../resume_shortlisting
PACKAGE_DIR: Path = Path(__file__).resolve().parent
# Project root: the directory that contains the package.
PROJECT_ROOT: Path = PACKAGE_DIR.parent

RESUMES_DIR: Path = PACKAGE_DIR / "resumes"
EXTRACTED_TEXT_DIR: Path = PACKAGE_DIR / "extracted_text"
OUTPUTS_DIR: Path = PACKAGE_DIR / "outputs"
LOGS_DIR: Path = PACKAGE_DIR / "logs"
DATA_DIR: Path = PACKAGE_DIR / "data"

SKILLS_FILE: Path = DATA_DIR / "skills.yaml"
LOG_FILE: Path = LOGS_DIR / "app.log"

# Default output artefact names (Step 19).
DEFAULT_EXCEL_OUTPUT: Path = OUTPUTS_DIR / "final_shortlisted.xlsx"
DEFAULT_JSON_OUTPUT: Path = OUTPUTS_DIR / "report.json"

# Directories that must exist before a run.
_MANAGED_DIRS: tuple[Path, ...] = (
    RESUMES_DIR,
    EXTRACTED_TEXT_DIR,
    OUTPUTS_DIR,
    LOGS_DIR,
    DATA_DIR,
)


def ensure_dirs() -> None:
    """Create all managed folders if they do not already exist."""
    for directory in _MANAGED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Column mapping (Step 1)
# --------------------------------------------------------------------------- #
# Maps a canonical field -> list of accepted header names (case-insensitive).
# The reader picks the first header present in the sheet, so real-world sheets
# with slightly different headers work without code changes.
COLUMN_ALIASES: dict[str, list[str]] = {
    "name": ["student name", "name", "full name", "candidate name", "student"],
    "email": ["email", "email id", "email address", "mail", "e-mail"],
    "resume_url": [
        "resume url",
        "resume link",
        "resume",
        "resume_url",
        "cv url",
        "cv link",
        "resume (pdf)",
        "share your updated resume drive link (give public access). ensure your resume includes your latest skills, projects.",
    ],
    # Optional, carried through to output if present.
    "phone": ["phone", "phone number", "mobile", "contact", "phone no"],
    "college": ["college", "university", "institute", "college name"],
    "branch": ["branch", "department", "stream", "specialization"],
}

# Canonical fields that MUST resolve to a column for a run to proceed.
REQUIRED_FIELDS: tuple[str, ...] = ("name", "resume_url")


# --------------------------------------------------------------------------- #
# Networking (Steps 2, 21)
# --------------------------------------------------------------------------- #
DOWNLOAD_TIMEOUT: int = int(os.getenv("RS_DOWNLOAD_TIMEOUT", "30"))  # seconds
DOWNLOAD_RETRIES: int = int(os.getenv("RS_DOWNLOAD_RETRIES", "3"))
DOWNLOAD_BACKOFF: float = float(os.getenv("RS_DOWNLOAD_BACKOFF", "1.5"))
DOWNLOAD_WORKERS: int = int(os.getenv("RS_DOWNLOAD_WORKERS", "16"))
MAX_RESUME_BYTES: int = int(os.getenv("RS_MAX_RESUME_BYTES", str(25 * 1024 * 1024)))

# Per-candidate processing (extract/parse/score) worker count.
PROCESS_WORKERS: int = int(os.getenv("RS_PROCESS_WORKERS", "8"))

USER_AGENT: str = (
    "Mozilla/5.0 (compatible; ResumeShortlister/1.0; +https://nxtwave.in)"
)

# --------------------------------------------------------------------------- #
# GitHub validation (Steps 9-10)
# --------------------------------------------------------------------------- #
GITHUB_TIMEOUT: int = int(os.getenv("RS_GITHUB_TIMEOUT", "15"))
GITHUB_RETRIES: int = int(os.getenv("RS_GITHUB_RETRIES", "2"))
# Optional token dramatically raises the unauthenticated rate limit
# (60/hr -> 5000/hr), which matters for 1000+ resumes. Never hard-code it.
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN") or None


# --------------------------------------------------------------------------- #
# Coding-profile evidence (information only, never scored)
# --------------------------------------------------------------------------- #
CODING_PROFILE_TIMEOUT: int = int(os.getenv("RS_CODING_PROFILE_TIMEOUT", "15"))


# --------------------------------------------------------------------------- #
# Scoring weights (Step 12) — fully configurable
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScoreWeights:
    """Weighted scoring configuration.

    The required-technology pool is split equally across the required keywords,
    so a missing keyword costs exactly its share and the system scales to any
    number of keywords. Project evidence is a single candidate-level bonus
    awarded at the strongest available tier, never once per keyword.
    """

    required_component_max: int = 80
    preferred_component_max: int = 10
    # Strongest tier wins; these never stack.
    project_github_max: int = 15
    # A deployed link alone is weaker evidence than a repository.
    project_live_ratio: float = 0.7
    # GitHub present in the resume but not attached to a matched project.
    github_elsewhere_max: int = 6
    linkedin_max: int = 5

    # Score is clamped to this inclusive range.
    min_score: int = 0
    max_score: int = 110


WEIGHTS = ScoreWeights()

# Frameworks and infrastructure tools require demonstrated project use. General
# languages can be configured per job in the UI and otherwise use these defaults.
KEYWORD_SEARCH_MODES: dict[str, str] = {
    "Python": "skills", "Java": "skills_or_project", "C++": "skills_or_project",
    "JavaScript": "skills_or_project", "SQL": "skills_or_project",
    "Django": "project", "FastAPI": "project", "Flask": "project",
    "React": "project", "Node.js": "project", "Docker": "project",
    "Redis": "project", "AWS": "project",
}


# --------------------------------------------------------------------------- #
# Recommendation bands (Step 13)
# --------------------------------------------------------------------------- #
# Ordered high -> low. First band whose threshold is <= score wins.
@dataclass(frozen=True)
class RecommendationBands:
    strong_shortlist: int = 80
    shortlist: int = 60
    consider: int = 40
    # Anything below `consider` -> "Reject".

    def classify(self, score: float, achievable: float = 100) -> str:
        """Band a score against what was actually achievable for this JD.

        Thresholds are percentages of ``achievable`` so they keep their meaning
        whatever the weights add up to (a JD with no preferred keywords simply
        has a lower ceiling). ``achievable`` defaults to 100, which reproduces
        the original absolute behaviour for callers that pass only a score.
        """
        percentage = (score / achievable * 100) if achievable > 0 else 0.0
        if percentage >= self.strong_shortlist:
            return "Strong Shortlist"
        if percentage >= self.shortlist:
            return "Shortlist"
        if percentage >= self.consider:
            return "Consider"
        return "Reject"


BANDS = RecommendationBands()


# --------------------------------------------------------------------------- #
# Section headings recognised by the resume parser (Step 4)
# --------------------------------------------------------------------------- #
# Canonical section -> list of heading synonyms (lower-case, matched loosely).
SECTION_HEADINGS: dict[str, list[str]] = {
    "summary": ["summary", "objective", "profile", "about me", "career objective"],
    "skills": [
        "skills",
        "technical skills",
        "core competencies",
        "technical proficiencies",
        "areas of expertise",
        "tech stack",
        "technologies",
    ],
    "projects": ["projects", "academic projects", "personal projects", "key projects"],
    "experience": [
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "internships",
        "internship experience",
    ],
    "education": ["education", "academic background", "qualifications", "academics"],
    "certifications": [
        "certifications",
        "certification",
        "courses",
        "licenses & certifications",
    ],
    "achievements": [
        "achievements",
        "accomplishments",
        "awards",
        "honors",
        "honours",
    ],
}

# Canonical sections returned by the parser's public dict (Step 4 contract).
PARSED_SECTION_KEYS: tuple[str, ...] = (
    "skills",
    "projects",
    "experience",
    "education",
    "certifications",
)


@dataclass
class Settings:
    """Aggregated settings object passed through the pipeline.

    Bundling settings into a single object keeps function signatures small and
    makes it trivial to override values in tests.
    """

    weights: ScoreWeights = field(default_factory=lambda: WEIGHTS)
    bands: RecommendationBands = field(default_factory=lambda: BANDS)
    resumes_dir: Path = RESUMES_DIR
    extracted_text_dir: Path = EXTRACTED_TEXT_DIR
    outputs_dir: Path = OUTPUTS_DIR
    skills_file: Path = SKILLS_FILE
    download_timeout: int = DOWNLOAD_TIMEOUT
    download_retries: int = DOWNLOAD_RETRIES
    download_backoff: float = DOWNLOAD_BACKOFF
    download_workers: int = DOWNLOAD_WORKERS
    process_workers: int = PROCESS_WORKERS
    github_timeout: int = GITHUB_TIMEOUT
    github_retries: int = GITHUB_RETRIES
    github_token: str | None = GITHUB_TOKEN


DEFAULT_SETTINGS = Settings()
