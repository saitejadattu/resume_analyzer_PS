"""Simple, explainable evidence-based scoring.

Shape of the score (see ``config.ScoreWeights`` for the numbers):

    required technologies   pool split equally across the required keywords
    project evidence        ONE candidate-level bonus at the strongest tier:
                            project repository > live link only > GitHub elsewhere
    preferred keywords      pool split equally across the preferred keywords
    LinkedIn                small presence signal

The search mode chosen for a keyword decides what counts as valid evidence, so
any match the matcher returns earns that keyword's full share; asking for
"Projects" is what stops a Skills-only mention from qualifying. Experience and
Whole Resume remain discovery-only and never score.

A candidate with no GitHub URL anywhere in the resume is rejected outright,
whatever the score. That gate tests for *presence* only — links are never
required to resolve, since GitHub validation is optional.
"""
from __future__ import annotations
from . import config
from .models import Candidate, GithubStatus, JDSpec, ParsedResume, ScoreResult
from .keyword_matcher import MatchReport

#: Match types that contribute points. The others are discovery-only.
SCORING_MATCH_TYPES = ("skills", "project")
#: Validation outcomes that disprove a repository link.
_DEAD_REPO = (GithubStatus.BROKEN, GithubStatus.NOT_FOUND)

NO_GITHUB_REMARK = "Rejected: no GitHub link was found anywhere in the resume."


def _evidence_remarks(jd: JDSpec, evidence: list, score: float) -> str:
    """Deterministically explain the actual keyword and project evidence."""
    by_skill = {item.skill.lower(): item for item in evidence if item.is_required}
    parts: list[str] = []
    missing: list[str] = []
    project_github: list[str] = []
    for skill in jd.required:
        item = by_skill.get(skill.lower())
        if item is None:
            missing.append(skill)
            continue
        if item.match_type == "skills":
            parts.append(f"{skill} found in Skills")
        elif item.match_type == "project":
            source = item.source.replace("_", " ") or "project"
            project = item.project_name or "an unnamed project"
            parts.append(f"{skill} demonstrated in {project} ({source})")
            if item.project_github_url:
                status = item.github_status
                if status is GithubStatus.WORKING:
                    project_github.append("Project GitHub repository was provided and validated as working")
                elif status is GithubStatus.NOT_CHECKED:
                    project_github.append("Project GitHub repository was provided but validation was skipped")
                elif status in (GithubStatus.BROKEN, GithubStatus.NOT_FOUND):
                    project_github.append("Project GitHub repository was provided but could not be validated")
                elif status is GithubStatus.PRIVATE:
                    project_github.append("Project GitHub repository appears to be private and could not be independently verified")
            elif item.project_live_url:
                project_github.append("No project GitHub repository was provided, but the project has a live deployment")
            else:
                project_github.append("No project GitHub repository was provided")
        elif item.match_type == "experience":
            parts.append(f"{skill} found in Experience (discovery only)")
        elif item.match_type == "whole_resume":
            parts.append(f"{skill} found in Whole Resume (discovery only)")
    if missing:
        parts.append(f"Missing required evidence: {', '.join(missing)}")
    # de-duplicate repeated GitHub wording when several keywords hit one project
    parts.extend(dict.fromkeys(project_github))
    if not parts:
        return "No required-keyword evidence was found because the resume could not be processed."
    if missing and score < 40:
        return "Rejected because " + "; ".join(parts) + "."
    return "; ".join(parts) + "."


def _project_evidence_score(scored_required: list, has_github_anywhere: bool, weights) -> float:
    """One candidate-level bonus, awarded at the strongest available tier.

    A repository beats a deployment, which beats a GitHub link that is not
    attached to any matched project. Tiers never stack, so the bonus cannot be
    multiplied by the number of keywords a single project happens to satisfy.
    """
    projects = [e for e in scored_required if e.match_type == "project"]
    # A link that validation actively disproved is not evidence; fall through.
    if any(e.project_github_url and e.github_status not in _DEAD_REPO for e in projects):
        return float(weights.project_github_max)
    if any(e.project_live_url for e in projects):
        return round(weights.project_github_max * weights.project_live_ratio, 1)
    if has_github_anywhere:
        return float(weights.github_elsewhere_max)
    return 0.0


def achievable_score(jd: JDSpec, weights=None) -> float:
    """Highest score this JD can produce, used to keep the bands meaningful."""
    weights = weights or config.DEFAULT_SETTINGS.weights
    return float(
        (weights.required_component_max if jd.required else 0)
        + weights.project_github_max
        + (weights.preferred_component_max if jd.preferred else 0)
        + weights.linkedin_max
    )


def score_candidate(candidate: Candidate, resume: ParsedResume, jd: JDSpec, match: MatchReport,
                    github_status: GithubStatus, github_urls: list[str], *, github_links=None,
                    github_checked=False, settings=None, kb=None, remarks="") -> ScoreResult:
    settings = settings or config.DEFAULT_SETTINGS
    w = settings.weights
    by_url = {link.url.rstrip("/").lower(): link.status for link in (github_links or [])}
    evidence = list(match.matches)
    for e in evidence:
        if e.match_type != "project": continue
        if not e.project_github_url: e.github_status = GithubStatus.NOT_PROVIDED
        elif not github_checked: e.github_status = GithubStatus.NOT_CHECKED
        else: e.github_status = by_url.get(e.project_github_url.rstrip("/").lower(), GithubStatus.BROKEN)
        e.verified = e.github_status is GithubStatus.WORKING

    # Each required keyword earns its full share; the search mode already
    # decided whether the evidence found was acceptable.
    scored_required = [e for e in evidence if e.is_required and e.match_type in SCORING_MATCH_TYPES]
    required_score = (len(scored_required) / len(jd.required) * w.required_component_max) if jd.required else 0.0

    preferred_hits = len([e for e in evidence if not e.is_required and e.match_type in SCORING_MATCH_TYPES])
    preferred_score = (preferred_hits / len(jd.preferred) * w.preferred_component_max) if jd.preferred else 0.0

    github_present = bool(resume.github_urls)
    project_score = _project_evidence_score(scored_required, github_present, w)
    linkedin_score = float(w.linkedin_max) if resume.linkedin_urls else 0.0

    score = max(w.min_score, required_score + preferred_score + project_score + linkedin_score)
    achievable = achievable_score(jd, w)
    # Mandatory gate: GitHub must exist somewhere, however the score lands.
    recommendation = settings.bands.classify(score, achievable) if github_present else "Reject"

    breakdown = {"required_keywords": round(required_score, 1), "preferred_keywords": round(preferred_score, 1),
                 "project_evidence": project_score, "linkedin": linkedin_score}
    explanation = remarks or _evidence_remarks(jd, evidence, score)
    if not github_present:
        explanation = f"{NO_GITHUB_REMARK} {explanation}"

    return ScoreResult(candidate=candidate, score=score, recommendation=recommendation, processing_status="Analyzed",
      matched_skills=match.matched_skills, matched_in=match.matched_in, missing_skills=match.missing_skills,
      keyword_evidence=evidence, candidate_github_urls=resume.candidate_github_urls,
      project_technologies=[t for p in resume.project_list for t in p.technologies],
      matched_projects=[e.project_name for e in scored_required if e.match_type == "project"],
      github_found=github_present, github_working=github_status is GithubStatus.WORKING,
      github_status=github_status, github_urls=github_urls, github_links=github_links or [], github_checked=github_checked,
      linkedin_urls=resume.linkedin_urls,
      projects=resume.project_list, remarks=explanation, score_breakdown=breakdown)
