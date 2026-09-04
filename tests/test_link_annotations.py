"""Hyperlink-annotation recovery.

Resume templates commonly render a GitHub/LeetCode link as an icon plus a bare
handle, leaving the real URL only in the file's link annotations.

Recovered URLs never reach ``raw_text`` (which Whole Resume matching searches)
or ``Project.github_url`` (which the project-evidence tier reads), so keyword
matching and project evidence are unaffected. They *do* legitimately change the
score, because GitHub presence is both a mandatory gate and a scored component:
a candidate whose only GitHub link was hidden in an annotation now passes the
gate instead of being rejected. That is the intended effect.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from resume_shortlisting import config
from resume_shortlisting.coding_profiles import discover_coding_profiles
from resume_shortlisting.downloader import DownloadResult
from resume_shortlisting.keyword_matcher import match_resume
from resume_shortlisting.models import Candidate, GithubStatus, JDSpec
from resume_shortlisting.parser import parse_resume
from resume_shortlisting.pipeline import process_candidate
from resume_shortlisting.scorer import score_candidate
from resume_shortlisting.skills_kb import load_kb
from resume_shortlisting.text_extractor import extract_link_urls

# Mirrors the real-world case: the visible text shows only bare handles.
RESUME = (
    "ADA LOVELACE\n"
    "ada@example.com | chirag-agarwal18 | ada_handle\n"
    "Skills\nPython, SQL, Django\n"
    "Projects\nHospital Management System\nBuilt with Django.\n"
    "Tech Stack: Django, PostgreSQL\n"
    "Experience\nWorked with n8n.\n"
)

HIDDEN_LINKS = [
    "https://github.com/ada_handle",
    "https://github.com/ada_handle/hospital",
    "https://leetcode.com/u/ada_lc/",
    "https://codeforces.com/profile/ada_cf",
    "https://www.codechef.com/users/ada_cc",
]


def _pdf_with_links(path: Path, text: str, urls: list[str]) -> None:
    """Write a one-page PDF whose URLs exist only as link annotations."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text.split("\n")[0])
    for index, url in enumerate(urls):
        rect = fitz.Rect(72, 100 + index * 20, 300, 116 + index * 20)
        page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})
    doc.save(path)
    doc.close()


class LinkExtractionTests(unittest.TestCase):
    def test_urls_are_recovered_from_pdf_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "resume.pdf"
            _pdf_with_links(pdf, RESUME, HIDDEN_LINKS)
            self.assertEqual(extract_link_urls(pdf), HIDDEN_LINKS)

    def test_duplicate_and_non_web_links_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "resume.pdf"
            _pdf_with_links(pdf, RESUME, [
                "https://github.com/ada_handle",
                "https://github.com/ada_handle/",
                "mailto:ada@example.com",
            ])
            self.assertEqual(extract_link_urls(pdf), ["https://github.com/ada_handle"])

    def test_a_pdf_without_links_yields_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "resume.pdf"
            _pdf_with_links(pdf, RESUME, [])
            self.assertEqual(extract_link_urls(pdf), [])

    def test_an_unreadable_file_never_raises(self):
        self.assertEqual(extract_link_urls(Path("does-not-exist.pdf")), [])
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "corrupt.pdf"
            corrupt.write_bytes(b"not a pdf at all")
            self.assertEqual(extract_link_urls(corrupt), [])


class LinkDiscoveryTests(unittest.TestCase):
    """Recovered links widen discovery without entering the matched text."""

    def setUp(self):
        self.kb = load_kb()
        self.without = parse_resume(RESUME)
        self.with_links = parse_resume(RESUME, HIDDEN_LINKS)

    def test_github_urls_are_found_only_once_links_are_supplied(self):
        self.assertEqual(self.without.github_urls, [])
        self.assertEqual(
            self.with_links.github_urls,
            ["https://github.com/ada_handle", "https://github.com/ada_handle/hospital"],
        )

    def test_candidate_profile_url_is_classified(self):
        self.assertEqual(
            self.with_links.candidate_github_urls, ["https://github.com/ada_handle"]
        )

    def test_raw_text_is_untouched_so_whole_resume_matching_is_unchanged(self):
        self.assertEqual(self.without.raw_text, self.with_links.raw_text)
        self.assertNotIn("github.com", self.with_links.raw_text)
        jd = JDSpec(required=["n8n"], search_modes={"n8n": "whole_resume"})
        self.assertEqual(
            [m.match_type for m in match_resume(self.without, jd, self.kb).matches],
            [m.match_type for m in match_resume(self.with_links, jd, self.kb).matches],
        )

    def test_project_github_url_is_not_populated_from_links(self):
        # The project-GitHub score component reads this; it must stay empty.
        self.assertEqual([p.github_url for p in self.with_links.project_list],
                         [p.github_url for p in self.without.project_list])
        self.assertTrue(all(not p.github_url for p in self.with_links.project_list))

    def test_coding_profiles_are_discovered_from_hidden_links(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada"),
            "\n".join([self.with_links.raw_text, *self.with_links.link_urls]),
        )
        self.assertEqual(profiles["leetcode"].profile_url, "https://leetcode.com/u/ada_lc/")
        self.assertEqual(profiles["codeforces"].profile_url, "https://codeforces.com/profile/ada_cf")
        self.assertEqual(profiles["codechef"].profile_url, "https://www.codechef.com/users/ada_cc")

    def test_parse_resume_still_accepts_a_single_argument(self):
        self.assertEqual(parse_resume(RESUME).github_urls, [])
        self.assertEqual(parse_resume(RESUME).link_urls, [])


class LinkScoringIsolationTests(unittest.TestCase):
    """Recovered links must not move the score or the recommendation."""

    def setUp(self):
        self.kb = load_kb()
        self.jd = JDSpec(
            required=["Python", "Django", "SQL"],
            search_modes={"Python": "skills", "Django": "project", "SQL": "skills_or_project"},
        )

    def _score(self, resume):
        report = match_resume(resume, self.jd, self.kb)
        return score_candidate(
            Candidate(name="Ada"), resume, self.jd, report,
            GithubStatus.NONE, resume.github_urls, github_checked=False,
        )

    def test_keyword_points_are_identical_and_only_github_evidence_changes(self):
        without = self._score(parse_resume(RESUME))
        with_links = self._score(parse_resume(RESUME, HIDDEN_LINKS))
        # Keyword scoring is driven by the visible text alone.
        self.assertEqual(without.score_breakdown["required_keywords"],
                         with_links.score_breakdown["required_keywords"])
        self.assertEqual(without.score_breakdown["preferred_keywords"],
                         with_links.score_breakdown["preferred_keywords"])
        # Only the GitHub-derived component moves, and only upwards.
        self.assertEqual(without.score_breakdown["project_evidence"], 0.0)
        self.assertEqual(with_links.score_breakdown["project_evidence"], 6.0)
        self.assertFalse(without.github_found)
        self.assertTrue(with_links.github_found)

    def test_a_hidden_github_link_rescues_a_candidate_from_the_gate(self):
        without = self._score(parse_resume(RESUME))
        with_links = self._score(parse_resume(RESUME, HIDDEN_LINKS))
        self.assertEqual(without.recommendation, "Reject")
        self.assertIn("no GitHub link", without.remarks)
        self.assertNotEqual(with_links.recommendation, "Reject")

    def test_pipeline_surfaces_hidden_links_without_changing_keyword_scoring(self):
        candidate = Candidate(name="Ada")
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "resume.pdf"
            _pdf_with_links(pdf, RESUME, HIDDEN_LINKS)
            download = DownloadResult(candidate, pdf, ok=True)
            with patch("resume_shortlisting.pipeline.extract_text", return_value=RESUME):
                result = process_candidate(
                    candidate, download, self.jd, self.kb, config.DEFAULT_SETTINGS,
                    check_github=False,
                )
        baseline = self._score(parse_resume(RESUME))
        # Keyword evidence is unchanged; only the GitHub component differs.
        self.assertEqual(result.score_breakdown["required_keywords"],
                         baseline.score_breakdown["required_keywords"])
        self.assertEqual(result.score, baseline.score + 6.0)
        # ...and the previously invisible evidence now shows up.
        self.assertIn("https://github.com/ada_handle", result.github_urls)
        self.assertTrue(result.coding_profiles["leetcode"].profile_found)
        self.assertTrue(result.coding_profiles["codeforces"].profile_found)
        self.assertTrue(result.coding_profiles["codechef"].profile_found)

    def test_link_extraction_failure_does_not_break_processing(self):
        candidate = Candidate(name="Ada")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as resume:
            download = DownloadResult(candidate, Path(resume.name), ok=True)
            with patch("resume_shortlisting.pipeline.extract_text", return_value=RESUME), patch(
                "resume_shortlisting.pipeline.extract_link_urls",
                side_effect=RuntimeError("unreadable"),
            ):
                result = process_candidate(
                    candidate, download, self.jd, self.kb, config.DEFAULT_SETTINGS,
                    check_github=False,
                )
        # Evidence recovery is best-effort: the candidate is still analyzed.
        self.assertEqual(result.processing_status, "Analyzed")
        baseline = self._score(parse_resume(RESUME))
        self.assertEqual(result.score, baseline.score)
        self.assertEqual(result.recommendation, baseline.recommendation)
        self.assertEqual(result.github_urls, [])


if __name__ == "__main__":
    unittest.main()
