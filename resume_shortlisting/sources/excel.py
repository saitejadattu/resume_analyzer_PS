"""Local Excel / CSV candidate source (Step 1, future-ready fallback)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import ResumeSource
from ..utils import get_logger

logger = get_logger("sources.excel")


class ExcelSource(ResumeSource):
    """Read candidates from a local ``.xlsx`` / ``.xls`` / ``.csv`` file."""

    def __init__(self, path: str | Path, *, sheet_name: str | int = 0) -> None:
        self.path = Path(path)
        self.sheet_name = sheet_name

    def load_dataframe(self) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(f"Excel file not found: {self.path}")

        logger.info("Reading local file: %s", self.path)
        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(self.path, dtype=str)
        return pd.read_excel(self.path, sheet_name=self.sheet_name, dtype=str)
