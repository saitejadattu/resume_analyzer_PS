"""Excel output (Step 14).

Writes the shortlisting results to a formatted ``.xlsx`` with exactly the
columns required by the spec, sorted by score (best first).
"""

from __future__ import annotations

from pathlib import Path
from datetime import date, datetime
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
import re

import pandas as pd

from .coding_profiles import TABLE_COLUMNS as CODING_COLUMNS, table_fields
from .experience import TABLE_COLUMNS as EXPERIENCE_COLUMNS
from .experience import table_fields as experience_fields
from .models import ScoreResult
from .utils import get_logger

logger = get_logger("excel_writer")

# Excel permits tab, line-feed and carriage-return, but not the remaining C0
# control range. PDF extraction can contain these invisible characters.
_ILLEGAL_EXCEL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def sanitize_excel_value(value: object) -> object:
    """Return a worksheet-safe value without mutating the source data.

    Keep normal Unicode (including replacement/checkmark/currency characters),
    newlines and tabs. Remove only characters that openpyxl/Excel cannot store,
    plus isolated surrogate code points which cannot be UTF-8 encoded.
    """
    value = serialize_for_sheet(value)
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    value = _ILLEGAL_EXCEL_CHARS.sub("", value)
    return "".join(char for char in value if not 0xD800 <= ord(char) <= 0xDFFF)


def serialize_for_sheet(value: object) -> object:
    """Convert arbitrary result/source values to a worksheet-safe scalar.

    Structured values remain structured through parsing and scoring. This is
    only an output-boundary conversion for XLSX/CSV/DataFrame consumers.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool, date, datetime)):
        return value
    if isinstance(value, Enum):
        return serialize_for_sheet(value.value)
    if hasattr(value, "model_dump"):
        return serialize_for_sheet(value.model_dump(mode="json"))
    if is_dataclass(value):
        return serialize_for_sheet(asdict(value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    if isinstance(value, (list, tuple, set)):
        return "; ".join(
            serialize_for_sheet(item) if not isinstance(item, (dict, list, tuple, set))
            else json.dumps(item, ensure_ascii=False, default=str, sort_keys=True)
            for item in value
        )
    return str(value)

# Column order per Step 14.
COLUMNS: list[str] = [
    "Student Name",
    "Email",
    "Resume URL",
    "Matched Skills",
    "Matched In",
    "Project Technologies",
    "GitHub Found",
    "GitHub Working",
    "GitHub URLs",
    "Matching Score",
    "Recommendation",
    "Processing Status",
    "Failure Reason",
    "Missing Skills",
    *CODING_COLUMNS,
    "Remarks",
    "Score Breakdown",
    "Keyword Evidence",
    *EXPERIENCE_COLUMNS,
]

FINAL_COLUMNS = [
    "Score", "Status", "Remarks", "Required Keywords Matched",
    "Required Keywords Missing", "Project Matches", "Skills Matches",
    "Project GitHub Status",
]


def final_status(result: ScoreResult) -> str:
    """User-facing final-sheet status, derived from the scored recommendation."""
    if result.processing_status != "Analyzed":
        return result.processing_status
    return "Rejected" if result.recommendation == "Reject" else result.recommendation


def _row(result: ScoreResult) -> dict[str, object]:
    """Flatten a ScoreResult into a spreadsheet row."""
    matched_in_str = "; ".join(
        f"{skill} ({', '.join(sections)})"
        for skill, sections in result.matched_in.items()
    )
    row = {
        "Student Name": result.candidate.display_name,
        "Email": result.candidate.email,
        "Resume URL": result.candidate.resume_url,
        "Matched Skills": ", ".join(result.matched_skills),
        "Matched In": matched_in_str,
        "Project Technologies": ", ".join(result.project_technologies),
        "GitHub Found": "Yes" if result.github_found else "No",
        "GitHub Working": "Yes" if result.github_working else "No",
        "GitHub URLs": "\n".join(result.github_urls),
        "Matching Score": round(result.score, 1) if result.score is not None else "N/A",
        "Recommendation": result.recommendation,
        "Processing Status": result.processing_status,
        "Failure Reason": result.failure_reason,
        "Missing Skills": ", ".join(result.missing_skills),
        # Solved count + profile URL per platform (informational, never scored).
        **table_fields(result.coding_profiles),
        "Remarks": result.remarks,
        "Score Breakdown": "; ".join(f"{key}: {value}" for key, value in result.score_breakdown.items()),
        "Keyword Evidence": "; ".join(
            f"{e.skill}: {e.match_type}; project={e.project_name}; source={e.source}; "
            f"github={e.project_github_url or 'Not Provided'}"
            + (f"; github_status={e.github_status.value}" if e.project_github_url else "")
            for e in result.keyword_evidence
        ),
        # One line per experience, aligned across the four columns.
        **experience_fields(result.experience_entries, result.total_experience),
    }
    return {key: sanitize_excel_value(value) for key, value in row.items()}


def to_dataframe(results: list[ScoreResult]) -> pd.DataFrame:
    """Flatten results into a score-sorted dataframe (shared by Excel + UI)."""
    ordered = sorted(results, key=lambda r: r.score if r.score is not None else -1, reverse=True)
    return pd.DataFrame([_row(r) for r in ordered], columns=COLUMNS)


def to_final_candidate_dataframe(results: list[ScoreResult]) -> pd.DataFrame:
    """Preserve source columns and append final shortlisting decisions.

    Source column order follows the input row retained on ``Candidate``. A
    fallback set keeps programmatic/legacy candidates useful too.
    """
    source_columns: list[str] = []
    for result in results:
        for column in result.candidate.source_data:
            if column not in source_columns:
                source_columns.append(column)
    if not source_columns:
        source_columns = ["Student Name", "Email", "Resume URL"]
    rows: list[dict[str, object]] = []
    for result in results:
        source = dict(result.candidate.source_data)
        if not source:
            source = {
                "Student Name": result.candidate.display_name,
                "Email": result.candidate.email,
                "Resume URL": result.candidate.resume_url,
            }
        row = {column: source.get(column, "") for column in source_columns}
        row.update({
            "Score": round(result.score, 1) if result.score is not None else "N/A",
            "Status": final_status(result),
            "Remarks": result.remarks,
            "Required Keywords Matched": ", ".join(
                evidence.skill for evidence in result.keyword_evidence if evidence.is_required
            ),
            "Required Keywords Missing": ", ".join(result.missing_skills),
            "Project Matches": "; ".join(
                f"{evidence.skill}: {evidence.project_name}" for evidence in result.keyword_evidence
                if evidence.is_required and evidence.match_type == "project"
            ),
            "Skills Matches": ", ".join(
                evidence.skill for evidence in result.keyword_evidence
                if evidence.is_required and evidence.match_type == "skills"
            ),
            "Project GitHub Status": "; ".join(
                f"{evidence.project_name or evidence.skill}: {evidence.github_status.value}"
                for evidence in result.keyword_evidence if evidence.match_type == "project"
            ),
        })
        rows.append({key: sanitize_excel_value(value) for key, value in row.items()})
    return pd.DataFrame(rows, columns=[*source_columns, *FINAL_COLUMNS])


def write_excel(results: list[ScoreResult], output_path: Path) -> Path:
    """Write results to an Excel workbook sorted by score desc (Step 14)."""
    df = to_dataframe(results)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Shortlist")
        _autofit(writer, df, "Shortlist")

    logger.info("Wrote Excel report (%d rows) -> %s", len(df), output_path)
    return output_path


def write_final_candidate_sheet(results: list[ScoreResult], output_path: Path) -> Path:
    """Write the all-candidates final sheet used by UI and CLI outputs."""
    df = to_final_candidate_dataframe(results)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Final Candidates")
        _autofit(writer, df, "Final Candidates")
    logger.info("Wrote final candidate sheet (%d rows) -> %s", len(df), output_path)
    return output_path


def _autofit(writer: pd.ExcelWriter, df: pd.DataFrame, sheet: str) -> None:
    """Best-effort column width auto-fit for readability."""
    try:
        worksheet = writer.sheets[sheet]
        for idx, col in enumerate(df.columns, start=1):
            # Cap width so long remark/url cells don't blow out the layout.
            max_len = max(
                len(str(col)),
                *(len(str(v)[:60]) for v in df[col].astype(str).tolist()),
            ) if len(df) else len(str(col))
            worksheet.column_dimensions[
                worksheet.cell(row=1, column=idx).column_letter
            ].width = min(max_len + 2, 50)
    except Exception as exc:  # noqa: BLE001 - formatting is non-critical
        logger.debug("Column autofit skipped: %s", exc)
