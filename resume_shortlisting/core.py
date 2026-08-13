"""Shared orchestration core used by both the CLI and the Streamlit UI.

Keeping the end-to-end run in one place means the web UI and the command line
exercise exactly the same pipeline (no duplicated logic — Step 20).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .downloader import download_all
from .excel_writer import write_final_candidate_sheet
from .jd_parser import parse_jd
from .json_writer import write_json
from .models import Candidate, JDSpec, ScoreResult
from .pipeline import process_all
from .skills_kb import SkillsKB, load_kb
from .sources import ResumeSource
from .utils import get_logger

logger = get_logger("core")


def build_jd_from_keywords(
    required: list[str],
    preferred: list[str] | None = None,
    kb: SkillsKB | None = None,
    search_modes: dict[str, str] | None = None,
) -> JDSpec:
    """Build a :class:`JDSpec` from explicit keyword lists (UI / --required).

    Tokens are normalised to canonical skill names when known, de-duplicated,
    and a keyword listed as both required and preferred stays required only.
    """
    kb = kb or load_kb()
    preferred = preferred or []

    def normalize(tokens: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in tokens:
            token = raw.strip()
            if not token:
                continue
            canonical = kb.canonical_of(token) or token
            key = canonical.lower()
            if key not in seen:
                seen.add(key)
                out.append(canonical)
        return out

    req = normalize(required)
    req_lower = {s.lower() for s in req}
    pref = [s for s in normalize(preferred) if s.lower() not in req_lower]
    supplied = {k.lower(): v for k, v in (search_modes or {}).items()}
    modes = {
        skill: supplied.get(skill.lower(), config.KEYWORD_SEARCH_MODES.get(skill, "skills_or_project"))
        for skill in req
    }
    return JDSpec(required=req, preferred=pref, search_modes=modes)


def parse_keyword_string(text: str) -> list[str]:
    """Split a free-form 'Python, Django; React' string into tokens."""
    if not text:
        return []
    parts: list[str] = []
    for chunk in text.replace(";", ",").replace("\n", ",").split(","):
        token = chunk.strip()
        if token:
            parts.append(token)
    return parts


@dataclass
class RunResult:
    """Everything a caller (CLI or UI) needs after a run."""

    results: list[ScoreResult]
    jd: JDSpec
    excel_path: Path | None = None
    json_path: Path | None = None
    stats: dict[str, int] = field(default_factory=dict)


def resolve_jd(
    *,
    jd_source: str | Path | None,
    required: list[str] | None,
    preferred: list[str] | None,
    kb: SkillsKB,
) -> JDSpec:
    """Decide the JD spec: explicit keywords take precedence over a JD file."""
    if required:
        logger.info("Using explicit required/preferred keywords")
        return build_jd_from_keywords(required, preferred or [], kb)
    if jd_source:
        return parse_jd(jd_source, kb)
    return JDSpec()


def run_shortlisting(
    *,
    source: ResumeSource,
    jd: JDSpec,
    settings: config.Settings | None = None,
    kb: SkillsKB | None = None,
    check_github: bool = True,
    limit: int = 0,
    write_outputs: bool = True,
    excel_out: Path | None = None,
    json_out: Path | None = None,
    download_progress=None,
    process_progress=None,
) -> RunResult:
    """Run the full pipeline against a source + JD and (optionally) write files.

    Progress callbacks receive ``(done, total)`` so a UI can render a bar.
    """
    settings = settings or config.DEFAULT_SETTINGS
    kb = kb or load_kb()
    config.ensure_dirs()

    candidates: list[Candidate] = source.read()
    if limit and limit > 0:
        candidates = candidates[:limit]

    downloads = download_all(candidates, settings, on_progress=download_progress)
    results = process_all(
        candidates,
        downloads,
        jd,
        kb,
        settings,
        check_github=check_github,
        on_progress=process_progress,
    )

    excel_path = json_path = None
    if write_outputs:
        excel_path = write_final_candidate_sheet(results, excel_out or config.DEFAULT_EXCEL_OUTPUT)
        json_path = write_json(results, json_out or config.DEFAULT_JSON_OUTPUT)

    stats: dict[str, int] = {}
    for r in results:
        stats[r.recommendation] = stats.get(r.recommendation, 0) + 1

    return RunResult(
        results=results,
        jd=jd,
        excel_path=excel_path,
        json_path=json_path,
        stats=stats,
    )
