"""Per-candidate processing pipeline (Steps 3-13, orchestrated in parallel).

Keeps ``main.py`` thin: this module owns the "process one candidate end-to-end"
logic and the concurrent fan-out. Every stage is guarded so a single bad resume
produces a low-scored result with an explanatory remark instead of aborting.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .downloader import DownloadResult
from .github_checker import summarize, validate_urls
from .keyword_matcher import match_resume
from .models import Candidate, GithubStatus, JDSpec, ScoreResult
from .parser import parse_resume
from .scorer import score_candidate
from .skills_kb import SkillsKB
from .tech_detector import enrich_projects
from .text_extractor import extract_text
from .utils import get_logger

logger = get_logger("pipeline")


def _failed_result(candidate: Candidate, jd: JDSpec, reason: str) -> ScoreResult:
    """Build a zero-ish result for a candidate we could not process."""
    return ScoreResult(
        candidate=candidate,
        score=0.0,
        recommendation="Reject",
        missing_skills=jd.all_skills(),
        github_status=GithubStatus.NONE,
        remarks=f"Not processed: {reason}",
    )


def process_candidate(
    candidate: Candidate,
    download: DownloadResult,
    jd: JDSpec,
    kb: SkillsKB,
    settings: config.Settings,
    *,
    check_github: bool = True,
) -> ScoreResult:
    """Run the full per-candidate pipeline (Steps 3-13)."""
    try:
        # Step 2 result: skip candidates whose resume never downloaded.
        if not download.ok or download.path is None:
            logger.info("[%s] Skipping — resume unavailable", candidate.display_name)
            return _failed_result(
                candidate, jd, download.error or "resume download failed"
            )

        # Step 3: extract text.
        text = extract_text(candidate, download.path)
        if not text:
            return _failed_result(
                candidate, jd, "could not extract text (empty/scanned PDF)"
            )

        # Step 4 + 9 + 11: parse sections, projects, github urls.
        resume = parse_resume(text)

        # Steps 7-8: detect per-project technologies (rule-based).
        enrich_projects(resume.project_list, kb)

        # Step 6: keyword matching across Skills + Projects.
        match = match_resume(resume, jd, kb)

        # Steps 9-10: validate GitHub links (keep per-link detail for the UI).
        github_status = GithubStatus.NONE
        github_links: list = []
        github_checked = False
        # Project repositories are verified independently. Candidate profiles
        # are display-only and never substitute for project proof or score.
        project_urls = list(dict.fromkeys(
            p.github_url for p in resume.project_list if p.github_url
        ))
        if project_urls and check_github:
            github_links = validate_urls(project_urls)
            github_status = summarize(github_links)
            github_checked = True

        # Steps 12-13: score + recommend.
        return score_candidate(
            candidate,
            resume,
            jd,
            match,
            github_status,
            resume.github_urls,
            github_links=github_links,
            github_checked=github_checked,
            settings=settings,
            kb=kb,
        )
    except Exception as exc:  # noqa: BLE001 - resilience: never crash the run
        logger.exception("[%s] Unexpected processing error", candidate.display_name)
        return _failed_result(candidate, jd, f"{type(exc).__name__}: {exc}")


def process_all(
    candidates: list[Candidate],
    downloads: dict[str, DownloadResult],
    jd: JDSpec,
    kb: SkillsKB,
    settings: config.Settings,
    *,
    check_github: bool = True,
    on_progress=None,
) -> list[ScoreResult]:
    """Process every candidate concurrently and return all results (Step 21).

    ``on_progress(done, total)`` is called after each candidate finishes
    (used by the UI progress bar).
    """
    if not candidates:
        return []

    workers = max(1, min(settings.process_workers, len(candidates)))
    total = len(candidates)
    logger.info("Processing %d candidate(s) with %d worker(s)", total, workers)

    results: list[ScoreResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for cand in candidates:
            download = downloads.get(
                cand.resume_url,
                DownloadResult(cand, None, ok=False, error="no download record"),
            )
            futures[
                pool.submit(
                    process_candidate,
                    cand,
                    download,
                    jd,
                    kb,
                    settings,
                    check_github=check_github,
                )
            ] = cand

        done = 0
        for future in as_completed(futures):
            cand = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - defensive
                logger.error("[%s] Worker crashed: %s", cand.display_name, exc)
                results.append(_failed_result(cand, jd, f"worker crash: {exc}"))
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    return results
