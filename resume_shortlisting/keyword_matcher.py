"""Evidence-first matching with per-keyword search modes."""

from __future__ import annotations

from .models import JDSpec, ParsedResume, SkillMatch
from .skills_kb import SkillsKB, load_kb

SECTION_SKILLS = "Skills"
SECTION_PROJECTS = "Projects"


class MatchReport:
    def __init__(self, matches: list[SkillMatch], jd: JDSpec) -> None:
        self.matches, self.jd = matches, jd

    @property
    def matched_skills(self): return [m.skill for m in self.matches]
    @property
    def matched_in(self):
        return {m.skill: [SECTION_PROJECTS] if m.match_type == "project" else [SECTION_SKILLS] for m in self.matches}
    @property
    def missing_skills(self):
        found = {m.skill.lower() for m in self.matches}
        return [s for s in self.jd.all_skills() if s.lower() not in found]
    def required_matches(self): return [m for m in self.matches if m.is_required]
    def preferred_matches(self): return [m for m in self.matches if not m.is_required]


def _project_match(skill: str, resume: ParsedResume, kb: SkillsKB, required: bool):
    for project in resume.project_list:
        # Project title and complete block include summary, descriptions and any
        # project-owned technology/tech-stack line.
        if kb.skill_in_text(skill, project.name): source, text = "project_title", project.name
        elif skill in project.technologies: source, text = "tech_stack", skill
        elif kb.skill_in_text(skill, project.description): source, text = "description", project.description
        else: continue
        return SkillMatch(skill=skill, is_required=required, match_type="project",
                          project_name=project.name, source=source, matched_text=text[:300],
                          project_github_url=project.github_url)
    return None


def _match_one(skill, required, resume, kb, mode):
    canonical = kb.canonical_of(skill) or skill
    project = _project_match(canonical, resume, kb, required)
    skills = kb.skill_in_text(canonical, resume.skills)
    if mode in ("project", "skills_or_project") and project:
        return project
    if mode in ("skills", "skills_or_project") and skills:
        return SkillMatch(skill=canonical, is_required=required, match_type="skills", source="skills", matched_text=resume.skills[:300])
    return None


def match_resume(resume: ParsedResume, jd: JDSpec, kb: SkillsKB | None = None) -> MatchReport:
    kb = kb or load_kb(); matches = []; seen = set()
    for skill in jd.required:
        result = _match_one(skill, True, resume, kb, jd.search_mode_for(skill))
        if result and result.skill.lower() not in seen: seen.add(result.skill.lower()); matches.append(result)
    for skill in jd.preferred:
        if skill.lower() in {s.lower() for s in jd.required}: continue
        result = _match_one(skill, False, resume, kb, "skills_or_project")
        if result and result.skill.lower() not in seen: seen.add(result.skill.lower()); matches.append(result)
    return MatchReport(matches, jd)
