import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_shortlisting import config
from resume_shortlisting.core import run_shortlisting
from resume_shortlisting.downloader import DownloadResult
from resume_shortlisting.models import Candidate, JDSpec, ScoreResult
from resume_shortlisting.pipeline import _failed_result, process_candidate
from resume_shortlisting.skills_kb import load_kb


class FrameSource:
    def __init__(self, candidates):
        self.candidates = candidates

    def read(self):
        return self.candidates


class ProcessingStatusTests(unittest.TestCase):
    def test_http_403_is_access_denied(self):
        candidate = Candidate(name="Private")
        result = process_candidate(
            candidate,
            DownloadResult(candidate, None, ok=False, error="HTTP 403: file inaccessible or permission denied"),
            JDSpec(),
            load_kb(),
            config.DEFAULT_SETTINGS,
            check_github=False,
        )
        self.assertEqual(result.processing_status, "Access Denied")
        self.assertIsNone(result.score)
        self.assertEqual(result.recommendation, "N/A")

    def test_empty_extraction_is_extraction_failed(self):
        candidate = Candidate(name="Unreadable")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as resume:
            download = DownloadResult(candidate, Path(resume.name), ok=True)
            with patch("resume_shortlisting.pipeline.extract_text", return_value=""):
                result = process_candidate(
                    candidate, download, JDSpec(required=["Python"]),
                    load_kb(),
                    config.DEFAULT_SETTINGS,
                    check_github=False,
                )
        self.assertEqual(result.processing_status, "Extraction Failed")
        self.assertIsNone(result.score)
        self.assertEqual(result.recommendation, "N/A")

    def test_run_stats_count_only_analyzed_recommendations(self):
        candidates = [Candidate(name="A"), Candidate(name="B"), Candidate(name="C")]
        analyzed = ScoreResult(candidate=candidates[0], score=80, recommendation="Strong Shortlist")
        rejected = ScoreResult(candidate=candidates[1], score=0, recommendation="Reject")
        failed = _failed_result(candidates[2], JDSpec(), "HTTP 404: file not found", status="Download Failed")
        with patch("resume_shortlisting.core.download_all", return_value={}), patch(
            "resume_shortlisting.core.process_all", return_value=[analyzed, rejected, failed]
        ):
            outcome = run_shortlisting(
                source=FrameSource(candidates), jd=JDSpec(), write_outputs=False
            )
        self.assertEqual(outcome.stats, {"Strong Shortlist": 1, "Reject": 1})
        self.assertEqual(outcome.status_counts, {"Analyzed": 2, "Download Failed": 1})
        self.assertEqual(sum(outcome.status_counts.values()), len(outcome.results))


if __name__ == "__main__":
    unittest.main()
