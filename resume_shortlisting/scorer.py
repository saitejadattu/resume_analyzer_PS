"""Simple, explainable evidence-based scoring."""
from __future__ import annotations
from . import config
from .models import Candidate, GithubStatus, JDSpec, ParsedResume, ScoreResult
from .keyword_matcher import MatchReport

def score_candidate(candidate: Candidate, resume: ParsedResume, jd: JDSpec, match: MatchReport,
                    github_status: GithubStatus, github_urls: list[str], *, github_links=None,
                    github_checked=False, settings=None, kb=None, remarks="") -> ScoreResult:
    settings = settings or config.DEFAULT_SETTINGS; w = settings.weights
    by_url = {link.url.rstrip("/").lower(): link.status for link in (github_links or [])}
    evidence = list(match.matches)
    for e in evidence:
        if e.match_type != "project": continue
        if not e.project_github_url: e.github_status = GithubStatus.NOT_PROVIDED
        elif not github_checked: e.github_status = GithubStatus.NOT_CHECKED
        else: e.github_status = by_url.get(e.project_github_url.rstrip("/").lower(), GithubStatus.BROKEN)
        e.verified = e.github_status is GithubStatus.WORKING
    required = [e for e in evidence if e.is_required]
    earned = sum(20 if e.match_type == "project" else 10 for e in required)
    required_score = (earned / (20 * len(jd.required)) * w.required_component_max) if jd.required else 0
    preferred = len([e for e in evidence if not e.is_required])
    preferred_score = (preferred / len(jd.preferred) * w.preferred_component_max) if jd.preferred else 0
    relevant = [e for e in required if e.match_type == "project" and e.project_github_url]
    statuses = {e.github_status for e in relevant}
    github_score = w.project_github_max if GithubStatus.WORKING in statuses else (w.project_github_unchecked if GithubStatus.NOT_CHECKED in statuses else 0)
    score = max(w.min_score, min(w.max_score, required_score + preferred_score + github_score))
    breakdown = {"required_keywords": round(required_score, 1), "preferred_keywords": round(preferred_score, 1), "project_github": github_score}
    return ScoreResult(candidate=candidate, score=score, recommendation=settings.bands.classify(score),
      matched_skills=match.matched_skills, matched_in=match.matched_in, missing_skills=match.missing_skills,
      keyword_evidence=evidence, candidate_github_urls=resume.candidate_github_urls,
      project_technologies=[t for p in resume.project_list for t in p.technologies],
      matched_projects=[e.project_name for e in required if e.match_type == "project"],
      github_found=bool(github_urls), github_working=github_status is GithubStatus.WORKING,
      github_status=github_status, github_urls=github_urls, github_links=github_links or [], github_checked=github_checked,
      projects=resume.project_list, remarks=remarks or f"{len(required)}/{len(jd.required)} required keywords matched", score_breakdown=breakdown)
