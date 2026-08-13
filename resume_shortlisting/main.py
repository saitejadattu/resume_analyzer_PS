"""CLI entry point for the Resume Shortlisting System (Steps 16, 19).

Usage:
    python -m resume_shortlisting.main --sheet <google-sheet-url> --jd jd.pdf
    python -m resume_shortlisting.main --excel students.xlsx --jd "Python, Django, React"

Outputs:
    outputs/final_shortlisted.xlsx
    outputs/report.json
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import config
from .core import parse_keyword_string, resolve_jd, run_shortlisting
from .skills_kb import load_kb
from .sources import ExcelSource, GoogleSheetSource
from .utils import get_logger, setup_logging


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resume_shortlisting",
        description="Shortlist candidates against a Job Description (keyword-based).",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--sheet", help="Public Google Sheet share URL of candidates."
    )
    source_group.add_argument(
        "--excel", help="Path to a local .xlsx/.csv file of candidates."
    )
    parser.add_argument(
        "--jd",
        help="Job Description: a .txt/.pdf/.docx path OR an inline skills string.",
    )
    parser.add_argument(
        "--required",
        help="Comma-separated required keywords (overrides --jd). "
        'e.g. --required "Python, Django, React"',
    )
    parser.add_argument(
        "--preferred",
        help='Comma-separated preferred keywords. e.g. --preferred "AWS, Redis"',
    )
    parser.add_argument(
        "--excel-out",
        default=str(config.DEFAULT_EXCEL_OUTPUT),
        help="Path for the Excel report (default: outputs/final_shortlisted.xlsx).",
    )
    parser.add_argument(
        "--json-out",
        default=str(config.DEFAULT_JSON_OUTPUT),
        help="Path for the JSON report (default: outputs/report.json).",
    )
    parser.add_argument(
        "--no-github",
        action="store_true",
        help="Skip GitHub validation (faster; avoids rate limits).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N candidates (0 = all). Useful for a dry run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the full pipeline. Returns a process exit code."""
    config.ensure_dirs()
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    logger = get_logger("main")
    settings = config.DEFAULT_SETTINGS
    start = time.perf_counter()

    # A JD file OR explicit --required keywords must be supplied.
    if not args.jd and not args.required:
        logger.error(
            'Provide a JD (--jd jd.pdf) or keywords (--required "Python, React").'
        )
        return 1

    # ---- Step 1: build the candidate source -------------------------------
    try:
        source = GoogleSheetSource(args.sheet) if args.sheet else ExcelSource(args.excel)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to build candidate source: %s", exc)
        return 2

    # ---- Step 5: resolve the JD (keywords override a JD file) -------------
    try:
        kb = load_kb()
        jd = resolve_jd(
            jd_source=args.jd,
            required=parse_keyword_string(args.required) if args.required else None,
            preferred=parse_keyword_string(args.preferred) if args.preferred else None,
            kb=kb,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve the Job Description / keywords: %s", exc)
        return 2

    if not jd.required and not jd.preferred:
        logger.error(
            "No skills to match. Provide a richer JD or keywords like: "
            '--required "Python, Django, React"'
        )
        return 1

    logger.info("Required skills: %s", ", ".join(jd.required) or "(none)")
    logger.info("Preferred skills: %s", ", ".join(jd.preferred) or "(none)")

    # ---- Steps 2, 3-13, 14-15: run the shared core ------------------------
    try:
        outcome = run_shortlisting(
            source=source,
            jd=jd,
            settings=settings,
            kb=kb,
            check_github=not args.no_github,
            limit=args.limit,
            excel_out=_resolve_out(args.excel_out),
            json_out=_resolve_out(args.json_out),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Run failed: %s", exc)
        return 2

    if not outcome.results:
        logger.error("No candidates were processed. Check the source sheet.")
        return 1

    elapsed = time.perf_counter() - start
    _print_summary(outcome.results, outcome.excel_path, outcome.json_path, elapsed)
    return 0


def _resolve_out(path_str: str):
    """Resolve an output path: a bare filename lands in the outputs dir."""
    from pathlib import Path

    p = Path(path_str)
    # Bare filename (no directory component) -> place under outputs/.
    if p.parent == Path("."):
        return config.OUTPUTS_DIR / p.name
    return p


def _print_summary(results, excel_path, json_path, elapsed: float) -> None:
    logger = get_logger("main")
    bands: dict[str, int] = {}
    for r in results:
        bands[r.recommendation] = bands.get(r.recommendation, 0) + 1

    logger.info("=" * 60)
    logger.info("Done in %.1fs — %d candidate(s) processed", elapsed, len(results))
    for band in ("Strong Shortlist", "Shortlist", "Consider", "Reject"):
        if band in bands:
            logger.info("  %-16s : %d", band, bands[band])
    logger.info("Excel : %s", excel_path)
    logger.info("JSON  : %s", json_path)
    logger.info("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
