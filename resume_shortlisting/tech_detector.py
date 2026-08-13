"""Per-project technology detection (Steps 7 & 8, rule-based — no LLM).

For every project we combine two signals:
    1. Direct skill hits    — canonical skills whose surface forms appear.
    2. Phrase inference      — technologies implied by descriptive language,
                               e.g. "retrieval augmented generation" -> RAG/GenAI/LLM.

This replaces the LLM step with a deterministic, editable rule engine. Improving
detection is a matter of extending ``data/skills.yaml`` — the module contract
stays the same, so an LLM-backed detector could be dropped in later (Step 22).
"""

from __future__ import annotations

from .models import Project
from .skills_kb import SkillsKB, load_kb
from .utils import get_logger

logger = get_logger("tech_detector")


def detect_project_technologies(
    project: Project, kb: SkillsKB | None = None
) -> list[str]:
    """Return the de-duplicated list of technologies used in one project."""
    kb = kb or load_kb()
    text = f"{project.name}\n{project.description}"

    techs: list[str] = []
    techs.extend(kb.detect_skills(text))
    techs.extend(kb.infer_from_phrases(text))

    # De-duplicate, preserve first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for tech in techs:
        if tech not in seen:
            seen.add(tech)
            ordered.append(tech)
    return ordered


def enrich_projects(
    projects: list[Project], kb: SkillsKB | None = None
) -> list[Project]:
    """Populate ``technologies`` on each project in-place and return the list."""
    kb = kb or load_kb()
    for project in projects:
        project.technologies = detect_project_technologies(project, kb)
    return projects


def all_project_technologies(projects: list[Project]) -> list[str]:
    """Union of technologies across all projects (Step 7 aggregate)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for project in projects:
        for tech in project.technologies:
            if tech not in seen:
                seen.add(tech)
                ordered.append(tech)
    return ordered
