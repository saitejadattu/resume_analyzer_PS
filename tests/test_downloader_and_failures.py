import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests

from resume_shortlisting import config
from resume_shortlisting.downloader import (
    _google_drive_file_id,
    download_resume,
)
from resume_shortlisting.excel_writer import to_dataframe, to_final_candidate_dataframe
from resume_shortlisting.models import Candidate, JDSpec
from resume_shortlisting.pipeline import _failed_result


FILE_ID = "FILE_ID"
DRIVE_URLS = [
    f"https://drive.google.com/file/d/{FILE_ID}/view",
    f"https://drive.google.com/open?id={FILE_ID}",
    f"https://drive.google.com/uc?id={FILE_ID}",
    f"https://drive.google.com/uc?export=download&id={FILE_ID}",
]


class FakeResponse:
    def __init__(self, content=b"%PDF-1.7 resume", content_type="application/pdf", status=200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.iter_content = lambda chunk_size: iter((content,))

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(response=self)
            raise error


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_resumes_dir = config.RESUMES_DIR
        config.RESUMES_DIR = Path(self.temp_dir.name)
        self.settings = config.Settings(
            resumes_dir=Path(self.temp_dir.name),
            download_workers=1,
            download_retries=1,
        )

    def tearDown(self):
        config.RESUMES_DIR = self.previous_resumes_dir
        self.temp_dir.cleanup()

    def test_supported_drive_urls_extract_file_id(self):
        self.assertEqual([_google_drive_file_id(url) for url in DRIVE_URLS], [FILE_ID] * 4)

    def test_supported_drive_urls_use_direct_download_endpoint(self):
        candidate = Candidate(name="Ana", resume_url=DRIVE_URLS[1])
        with patch("resume_shortlisting.downloader.requests.get", return_value=FakeResponse()) as get:
            result = download_resume(candidate, self.settings)
        self.assertTrue(result.ok)
        self.assertIn(f"id={FILE_ID}", get.call_args.args[0])
        self.assertIn("drive.usercontent.google.com", get.call_args.args[0])

    def test_valid_pdf_and_docx_are_accepted(self):
        cases = [
            (b"%PDF-1.7 resume", "application/pdf"),
            (b"PK\x03\x04docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ]
        for content, content_type in cases:
            with self.subTest(content_type=content_type):
                candidate = Candidate(name="Ana", resume_url=f"https://example.com/{content_type}")
                with patch(
                    "resume_shortlisting.downloader.requests.get",
                    return_value=FakeResponse(content, content_type),
                ):
                    result = download_resume(candidate, self.settings)
                self.assertTrue(result.ok)
                self.assertIsNotNone(result.path)

    def test_html_response_is_download_failure(self):
        candidate = Candidate(name="Ana", resume_url=DRIVE_URLS[1])
        response = FakeResponse(b"<html>Drive page</html>", "text/html; charset=utf-8")
        with patch("resume_shortlisting.downloader.requests.get", return_value=response):
            result = download_resume(candidate, self.settings)
        self.assertFalse(result.ok)
        self.assertIn("supported PDF/DOCX/DOC", result.error)

    def test_http_failures_preserve_specific_reason(self):
        for status, expected in ((403, "inaccessible"), (404, "not found"), (429, "rate limited")):
            with self.subTest(status=status):
                candidate = Candidate(name="Ana", resume_url=DRIVE_URLS[1] + str(status))
                with patch(
                    "resume_shortlisting.downloader.requests.get",
                    return_value=FakeResponse(status=status),
                ):
                    result = download_resume(candidate, self.settings)
                self.assertFalse(result.ok)
                self.assertIn(f"HTTP {status}", result.error)
                self.assertIn(expected, result.error)


class FailureResultTests(unittest.TestCase):
    def test_download_failure_is_rejected_but_keeps_its_reason(self):
        result = _failed_result(
            Candidate(name="Ana", resume_url="https://example.com/resume.pdf"),
            JDSpec(required=["Python"]),
            "HTTP 404: file not found",
            status="Download Failed",
        )
        # The mandatory GitHub gate rejects a candidate we could not read, but
        # the processing status still says why, so a broken link stays
        # distinguishable from a genuinely weak CV.
        self.assertIsNone(result.score)
        self.assertEqual(result.recommendation, "Reject")
        self.assertEqual(result.processing_status, "Download Failed")
        self.assertEqual(result.failure_reason, "HTTP 404: file not found")
        self.assertEqual(to_dataframe([result]).loc[0, "Matching Score"], "N/A")
        final = to_final_candidate_dataframe([result])
        self.assertEqual(final.loc[0, "Status"], "Download Failed")


if __name__ == "__main__":
    unittest.main()
