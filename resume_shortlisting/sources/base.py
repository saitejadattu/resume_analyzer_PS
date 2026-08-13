"""Abstract base for candidate data sources (Step 22).

The rest of the pipeline depends only on ``ResumeSource.read() -> list[Candidate]``,
so swapping Excel for a Google Sheet, S3 manifest, or database never touches the
scoring/matching code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import re

import pandas as pd

from .. import config
from ..models import Candidate
from ..utils import get_logger

logger = get_logger("sources")


class ResumeSource(ABC):
    """Base class every candidate source must implement."""

    @abstractmethod
    def load_dataframe(self) -> pd.DataFrame:
        """Return a raw dataframe of candidate rows (subclass responsibility)."""

    def read(self) -> list[Candidate]:
        """Load rows and map them to :class:`Candidate` objects (Step 1)."""
        df = self.load_dataframe()
        df = self._normalize_headers(df)
        resolved = self._resolve_columns(df)
        self._validate_required(resolved, list(df.columns))
        return self._to_candidates(df, resolved)

    # ------------------------------------------------------------------ #
    # Header / column resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
        """Preserve source headers while removing only surrounding whitespace.

        Matching is performed separately by :meth:`_header_key`, so exported
        talent-pool data retains the spreadsheet's original header spelling.
        """
        df = df.copy()
        df.columns = [str(c).strip() for c in df.columns]
        return df

    @staticmethod
    def _header_key(value: str) -> str:
        """Return a conservative comparison key for a spreadsheet header.

        This handles Google Forms line wraps, tabs, case and harmless
        punctuation variations without doing fuzzy/substring matching.
        """
        value = str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")
        value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
        return " ".join(value.casefold().split())

    @staticmethod
    def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
        """Map canonical field -> actual column header using COLUMN_ALIASES."""
        key_to_actual = {ResumeSource._header_key(str(c)): str(c) for c in df.columns}
        resolved: dict[str, str] = {}
        for field, aliases in config.COLUMN_ALIASES.items():
            for alias in aliases:
                actual = key_to_actual.get(ResumeSource._header_key(alias))
                if actual is not None:
                    resolved[field] = actual
                    break
        return resolved

    @staticmethod
    def _validate_required(resolved: dict[str, str], headers: list[str] | None = None) -> None:
        """Raise if a required canonical field could not be resolved."""
        missing = [f for f in config.REQUIRED_FIELDS if f not in resolved]
        if missing:
            raise ValueError(
                "Could not find required column(s) "
                f"{missing} in the sheet. Accepted header names: "
                + "; ".join(
                    f"{f} -> {config.COLUMN_ALIASES[f]}" for f in missing
                )
                + ". Detected headers: " + ", ".join(headers or [])
            )

    @staticmethod
    def _to_candidates(
        df: pd.DataFrame, resolved: dict[str, str]
    ) -> list[Candidate]:
        """Convert dataframe rows into Candidate models (Step 1)."""
        candidates: list[Candidate] = []
        known_cols = set(resolved.values())

        for _, row in df.iterrows():
            def cell(field: str) -> str:
                col = resolved.get(field)
                if col is None:
                    return ""
                value = row.get(col, "")
                if pd.isna(value):
                    return ""
                return str(value).strip()

            name = cell("name")
            resume_url = cell("resume_url")

            # Skip completely empty rows.
            if not name and not resume_url:
                continue

            # Preserve every other column verbatim for the output.
            extra: dict[str, str] = {}
            for col in df.columns:
                if col in known_cols:
                    continue
                value = row.get(col, "")
                if pd.isna(value):
                    continue
                extra[col] = str(value).strip()

            candidates.append(
                Candidate(
                    name=name,
                    email=cell("email"),
                    resume_url=resume_url,
                    extra=extra,
                    source_data={
                        str(col): "" if pd.isna(row.get(col, "")) else str(row.get(col, "")).strip()
                        for col in df.columns
                    },
                )
            )

        logger.info("Loaded %d candidate row(s) from source", len(candidates))
        return candidates
