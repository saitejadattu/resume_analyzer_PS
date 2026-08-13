"""Pydantic data models shared across the pipeline (Step 20).

These models are the contract between stages. Using Pydantic gives us
validation, sane defaults, and effortless JSON serialisation for the report.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GithubStatus(str, Enum):
    """Result of validating a single GitHub URL (Step 10)."""

    WORKING = "Working"
    BROKEN = "Broken"
    PRIVATE = "Private"
    NOT_FOUND = "Not Found"
    NONE = "None"
    NOT_PROVIDED = "Not Provided"
    NOT_CHECKED = "Not Checked"


class GithubKind(str, Enum):
    """Whether a GitHub URL points at a profile or a repository (Step 10)."""

    PROFILE = "profile"
    REPOSITORY = "repository"
    UNKNOWN = "unknown"


class Candidate(BaseModel):
    """A single row read from the source sheet (Step 1)."""

    name: str
    email: str = ""
    resume_url: str = ""
    # Any additional columns (phone, college, branch, ...) preserved verbatim.
    extra: dict[str, str] = Field(default_factory=dict)
    # Original source row, retained so the final candidate sheet can preserve
    # the input columns and their original header names.
    source_data: dict[str, str] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.email or "Unknown"


class GithubLink(BaseModel):
    """A GitHub URL together with its validation result (Steps 9-10)."""

    url: str
    kind: GithubKind = GithubKind.UNKNOWN
    status: GithubStatus = GithubStatus.NONE


class Project(BaseModel):
    """A single project parsed from the resume (Steps 7, 11)."""

    name: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    github_url: str = ""
    live_url: str = ""


class ParsedResume(BaseModel):
    """Structured view of a resume after parsing (Steps 3-4, 7, 9)."""

    # Canonical sections (Step 4 contract). Missing -> empty string.
    skills: str = ""
    projects: str = ""
    experience: str = ""
    education: str = ""
    certifications: str = ""

    # Richer extracted structures.
    project_list: list[Project] = Field(default_factory=list)
    github_urls: list[str] = Field(default_factory=list)
    candidate_github_urls: list[str] = Field(default_factory=list)
    raw_text: str = ""

    def sections_dict(self) -> dict[str, str]:
        """Return the Step-4 five-section contract as a plain dict."""
        return {
            "skills": self.skills,
            "projects": self.projects,
            "experience": self.experience,
            "education": self.education,
            "certifications": self.certifications,
        }


class JDSpec(BaseModel):
    """Parsed Job Description (Step 5)."""

    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    search_modes: dict[str, str] = Field(default_factory=dict)

    def search_mode_for(self, skill: str) -> str:
        return self.search_modes.get(skill, self.search_modes.get(skill.lower(), "skills_or_project"))

    def all_skills(self) -> list[str]:
        """Required + preferred, de-duplicated, order preserved."""
        seen: set[str] = set()
        combined: list[str] = []
        for skill in [*self.required, *self.preferred]:
            key = skill.lower()
            if key not in seen:
                seen.add(key)
                combined.append(skill)
        return combined


class SkillMatch(BaseModel):
    """Where a JD skill was found in a resume (Step 6)."""

    skill: str
    # Sections the skill was found in, e.g. ["Skills", "Projects"].
    matched_in: list[str] = Field(default_factory=list)
    is_required: bool = True
    match_type: str = "skills"
    project_name: str = ""
    source: str = ""
    matched_text: str = ""
    project_github_url: str = ""
    github_status: GithubStatus = GithubStatus.NOT_PROVIDED
    verified: bool = False

    @property
    def in_projects(self) -> bool:
        return "Projects" in self.matched_in

    @property
    def in_skills(self) -> bool:
        return "Skills" in self.matched_in


class ScoreResult(BaseModel):
    """Final per-candidate result (Steps 12-15)."""

    candidate: Candidate

    score: float = 0.0
    recommendation: str = "Reject"

    matched_skills: list[str] = Field(default_factory=list)
    # skill -> ["Skills", "Projects"]
    matched_in: dict[str, list[str]] = Field(default_factory=dict)
    missing_skills: list[str] = Field(default_factory=list)
    keyword_evidence: list[SkillMatch] = Field(default_factory=list)
    candidate_github_urls: list[str] = Field(default_factory=list)

    project_technologies: list[str] = Field(default_factory=list)
    matched_projects: list[str] = Field(default_factory=list)

    github_found: bool = False
    github_working: bool = False
    github_status: GithubStatus = GithubStatus.NONE
    github_urls: list[str] = Field(default_factory=list)
    # Per-URL validation detail (for the visual GitHub breakdown).
    github_links: list[GithubLink] = Field(default_factory=list)
    # Whether GitHub links were actually validated this run.
    github_checked: bool = False

    # Full parsed projects (name, technologies, links) for the visual detail.
    projects: list[Project] = Field(default_factory=list)

    # Human-readable explanation of the score / any processing failures.
    remarks: str = ""
    # Itemised score breakdown for transparency/debugging.
    score_breakdown: dict[str, float] = Field(default_factory=dict)

    def to_report_dict(self) -> dict:
        """Compact JSON shape for the report (Step 15)."""
        return {
            "student": self.candidate.display_name,
            "email": self.candidate.email,
            "resume_url": self.candidate.resume_url,
            "score": round(self.score, 1),
            "matched_skills": self.matched_skills,
            "keyword_evidence": [e.model_dump(mode="json") for e in self.keyword_evidence],
            "candidate_github_urls": self.candidate_github_urls,
            "matched_projects": self.matched_projects,
            "project_technologies": self.project_technologies,
            "github_status": self.github_status.value,
            "github_urls": self.github_urls,
            "github_links": [
                {"url": link.url, "kind": link.kind.value, "status": link.status.value}
                for link in self.github_links
            ],
            "projects": [
                {
                    "name": p.name,
                    "technologies": p.technologies,
                    "github_url": p.github_url,
                    "live_url": p.live_url,
                }
                for p in self.projects
            ],
            "missing_skills": self.missing_skills,
            "recommendation": self.recommendation,
            "remarks": self.remarks,
            "score_breakdown": self.score_breakdown,
        }
