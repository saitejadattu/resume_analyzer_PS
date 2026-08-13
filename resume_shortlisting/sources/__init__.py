"""Pluggable candidate data sources (Step 22).

New sources (Google Drive listing, raw S3 manifest, database, ...) can be added
by subclassing :class:`ResumeSource` without changing any business logic.
"""

from __future__ import annotations

from .base import ResumeSource
from .excel import ExcelSource
from .google_sheet import GoogleSheetSource
from .talent_pool import TalentPoolSource

__all__ = ["ResumeSource", "ExcelSource", "GoogleSheetSource", "TalentPoolSource", "build_source"]


def build_source(spec: str) -> ResumeSource:
    """Factory: choose a source implementation from a CLI argument.

    A Google Sheets URL is routed to :class:`GoogleSheetSource`; anything else
    is treated as a local Excel path (:class:`ExcelSource`).
    """
    lowered = spec.lower()
    if "docs.google.com/spreadsheets" in lowered:
        return GoogleSheetSource(spec)
    return ExcelSource(spec)
