import unittest

import pandas as pd

from resume_shortlisting.sources.base import ResumeSource
from resume_shortlisting.sources.talent_pool import TalentPoolSource


LONG_HEADER = "Share your Updated Resume Drive link ( Give Public Access ) Ensure your resume includes your latest skills, projects."


class FrameSource(ResumeSource):
    def __init__(self, frame): self.frame = frame
    def load_dataframe(self): return self.frame


class SourceHeaderTests(unittest.TestCase):
    def _read(self, header):
        return FrameSource(pd.DataFrame({"Student Name": ["Ana"], header: ["https://example.com/a.pdf"]})).read()[0]

    def test_long_resume_header_variations_resolve(self):
        variants = [
            LONG_HEADER,
            "Share your Updated Resume Drive link ( Give Public Access )\nEnsure your resume includes your latest skills, projects.",
            "  Share   your Updated Resume Drive link ( Give Public Access )   Ensure your resume includes your latest skills, projects.  ",
            "share your updated resume drive link ( give public access ) ensure your resume includes your latest skills, projects",
            "resume_url",
        ]
        for header in variants:
            with self.subTest(header=header):
                self.assertEqual(self._read(header).resume_url, "https://example.com/a.pdf")

    def test_missing_resume_header_reports_detected_headers_and_aliases(self):
        source = FrameSource(pd.DataFrame({"Student Name": ["Ana"], "Portfolio URL": ["https://example.com"]}))
        with self.assertRaises(ValueError) as caught:
            source.read()
        message = str(caught.exception)
        self.assertIn("resume_url", message)
        self.assertIn("Portfolio URL", message)
        self.assertIn("share your updated resume drive link", message.casefold())

    def test_normalized_talent_pool_schema_uses_student_name_not_name(self):
        frame = pd.DataFrame({
            "student_uid": ["S-1"], "student_name": ["Ana"],
            "email": ["ana@example.com"], "resume_url": ["https://example.com/a.pdf"],
            "technologies": ["React, Node.js"],
        })
        candidate = TalentPoolSource(FrameSource(frame)).read()[0]
        self.assertEqual(candidate.name, "Ana")
        self.assertEqual(candidate.email, "ana@example.com")
        self.assertEqual(candidate.resume_url, "https://example.com/a.pdf")
        self.assertEqual(candidate.source_data["student_uid"], "S-1")

    def test_talent_pool_reports_normalized_missing_fields(self):
        frame = pd.DataFrame({"student_uid": ["S-1"], "name": ["Ana"], "email": ["ana@example.com"], "resume_url": ["https://example.com/a.pdf"]})
        with self.assertRaisesRegex(ValueError, "student_name"):
            TalentPoolSource(FrameSource(frame)).read()


if __name__ == "__main__": unittest.main()
