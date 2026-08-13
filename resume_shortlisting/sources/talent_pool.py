"""Reader for the canonical, normalized Student Talent Pool sheet schema."""
from __future__ import annotations

import pandas as pd

from .base import ResumeSource
from ..models import Candidate


class TalentPoolSource:
    """Adapt an Excel/Google-Sheet source to the Talent Pool schema only.

    This deliberately does not reuse the Resume Analyzer's ``name`` contract:
    the normalized student sheet's canonical identity column is
    ``student_name``.
    """
    REQUIRED_FIELDS = ("student_uid", "student_name", "email", "resume_url")

    def __init__(self, source: ResumeSource) -> None:
        self.source = source

    def read(self) -> list[Candidate]:
        frame = self.source.load_dataframe().copy()
        frame.columns = [str(column).strip() for column in frame.columns]
        actual_by_key = {ResumeSource._header_key(column): column for column in frame.columns}
        resolved = {field: actual_by_key.get(ResumeSource._header_key(field)) for field in self.REQUIRED_FIELDS}
        missing = [field for field, column in resolved.items() if column is None]
        if missing:
            raise ValueError(
                f"Talent Pool sheet is missing required field(s) {missing}. "
                f"Detected headers: {', '.join(frame.columns)}. "
                "Required normalized headers: student_uid, student_name, email, resume_url."
            )

        candidates: list[Candidate] = []
        for _, row in frame.iterrows():
            def value(field: str) -> str:
                item = row.get(resolved[field], "")
                return "" if pd.isna(item) else str(item).strip()
            if not any(value(field) for field in self.REQUIRED_FIELDS):
                continue
            candidates.append(Candidate(
                name=value("student_name"), email=value("email"), resume_url=value("resume_url"),
                source_data={column: "" if pd.isna(row.get(column, "")) else str(row.get(column, "")).strip() for column in frame.columns},
            ))
        return candidates
