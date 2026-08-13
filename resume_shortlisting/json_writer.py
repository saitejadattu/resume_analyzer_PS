"""JSON report output (Step 15)."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ScoreResult
from .utils import get_logger

logger = get_logger("json_writer")


def write_json(results: list[ScoreResult], output_path: Path) -> Path:
    """Write a JSON report, best-scored candidate first (Step 15)."""
    ordered = sorted(results, key=lambda r: r.score, reverse=True)
    payload = {
        "total_candidates": len(ordered),
        "shortlisted": sum(
            1 for r in ordered if r.recommendation in ("Strong Shortlist", "Shortlist")
        ),
        "results": [r.to_report_dict() for r in ordered],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Wrote JSON report (%d results) -> %s", len(ordered), output_path)
    return output_path
