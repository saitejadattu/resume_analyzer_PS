"""Google Sheets candidate source (Step 1 + Step 22).

Reads a *shared* Google Sheet directly, with no API credentials, by converting
the share link into the public XLSX export endpoint:

    https://docs.google.com/spreadsheets/d/<ID>/export?format=xlsx[&gid=<GID>]

The sheet must be viewable by "Anyone with the link". For private sheets, swap
this class for a ``gspread``-based implementation without touching the pipeline.
"""

from __future__ import annotations

import io
import re

import pandas as pd
import requests

from .. import config
from .base import ResumeSource
from ..utils import get_logger

logger = get_logger("sources.google_sheet")

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_GID_RE = re.compile(r"[#&?]gid=([0-9]+)")


class GoogleSheetSource(ResumeSource):
    """Read candidates from a public Google Sheet share URL."""

    def __init__(self, url: str) -> None:
        self.url = url.strip()
        self.sheet_id = self._extract_sheet_id(self.url)
        self.gid = self._extract_gid(self.url)

    @staticmethod
    def _extract_sheet_id(url: str) -> str:
        match = _SHEET_ID_RE.search(url)
        if not match:
            raise ValueError(
                "Could not extract a spreadsheet ID from the Google Sheet URL. "
                "Expected a link like "
                "https://docs.google.com/spreadsheets/d/<ID>/edit"
            )
        return match.group(1)

    @staticmethod
    def _extract_gid(url: str) -> str | None:
        match = _GID_RE.search(url)
        return match.group(1) if match else None

    @property
    def export_url(self) -> str:
        """Public XLSX export endpoint for the sheet/tab."""
        base = (
            f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/export"
            f"?format=xlsx"
        )
        if self.gid:
            base += f"&gid={self.gid}"
        return base

    def load_dataframe(self) -> pd.DataFrame:
        logger.info("Fetching Google Sheet export: %s", self.export_url)
        try:
            resp = requests.get(
                self.export_url,
                timeout=config.DOWNLOAD_TIMEOUT,
                headers={"User-Agent": config.USER_AGENT},
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                "Failed to fetch the Google Sheet. Ensure it is shared as "
                f"'Anyone with the link can view'. Underlying error: {exc}"
            ) from exc

        # A permission wall returns HTML, not an XLSX binary.
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise RuntimeError(
                "Google returned an HTML page instead of a spreadsheet. The "
                "sheet is likely private — set sharing to "
                "'Anyone with the link can view'."
            )

        return pd.read_excel(io.BytesIO(resp.content), dtype=str)
