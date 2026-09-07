"""Structured Experience-section extraction.

Informational only: none of it may reach the score or the recommendation.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from resume_shortlisting import config
from resume_shortlisting.downloader import DownloadResult
from resume_shortlisting.experience import (
    explicit_technologies,
    extract_entries,
    extract_experience,
    format_duration,
    parse_date,
    total_experience_months,
)
from resume_shortlisting.keyword_matcher import match_resume
from resume_shortlisting.models import Candidate, GithubStatus, JDSpec
from resume_shortlisting.parser import parse_resume
from resume_shortlisting.pipeline import process_candidate
from resume_shortlisting.scorer import score_candidate
from resume_shortlisting.skills_kb import load_kb

TWO_ROLES = (
    "Software Engineer, ABC Technologies\n"
    "Jan 2024 - Aug 2026\n"
    "- Built services with React, Node.js and MongoDB.\n"
    "Data Analyst Intern, XYZ Solutions Pvt Ltd\n"
    "06/2023 - Dec 2023\n"
    "- Worked as a Software Engineer developing web applications.\n"
)

# Django/PostgreSQL appear only in Skills and Projects, never in Experience.
FULL_RESUME = (
    "Skills\nPython, Django, PostgreSQL, Kubernetes\n"
    "Projects\nHospital System\nTech Stack: Django, PostgreSQL\n"
    "GitHub: https://github.com/ada/hospital\n"
    "Experience\n" + TWO_ROLES
)


class DateParsingTests(unittest.TestCase):
    def test_common_resume_date_formats(self):
        self.assertEqual(parse_date("Jan 2024"), (2024, 1))
        self.assertEqual(parse_date("January 2024"), (2024, 1))
        self.assertEqual(parse_date("Jan'24"), (2024, 1))
        self.assertEqual(parse_date("06/2023"), (2023, 6))
        self.assertEqual(parse_date("Aug 2026"), (2026, 8))

    def test_year_only_opens_in_january_and_closes_in_december(self):
        self.assertEqual(parse_date("2023"), (2023, 1))
        self.assertEqual(parse_date("2025", is_end=True), (2025, 12))

    def test_present_resolves_to_today(self):
        today = date.today()
        for token in ("Present", "present", "Current", "Till date"):
            self.assertEqual(parse_date(token), (today.year, today.month))

    def test_unparseable_text_returns_none(self):
        self.assertIsNone(parse_date("sometime last year"))
        self.assertIsNone(parse_date(""))

    def test_duration_formatting(self):
        self.assertEqual(format_duration(32), "2 years 8 months")
        self.assertEqual(format_duration(12), "1 year")
        self.assertEqual(format_duration(7), "7 months")
        self.assertEqual(format_duration(None), "Not available")


class EntryExtractionTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_kb()
        self.entries = extract_entries(TWO_ROLES, self.kb)

    def test_each_role_becomes_one_entry(self):
        self.assertEqual(len(self.entries), 2)

    def test_company_is_extracted(self):
        self.assertEqual(self.entries[0].company, "ABC Technologies")
        self.assertEqual(self.entries[1].company, "XYZ Solutions Pvt Ltd")

    def test_role_is_extracted(self):
        self.assertEqual(self.entries[0].role, "Software Engineer")
        self.assertEqual(self.entries[1].role, "Data Analyst Intern")

    def test_dates_and_duration_are_extracted(self):
        first = self.entries[0]
        self.assertEqual(first.start_date, "Jan 2024")
        self.assertEqual(first.end_date, "Aug 2026")
        self.assertEqual(first.duration_months, 32)
        self.assertEqual(first.duration, "2 years 8 months")
        self.assertEqual(self.entries[1].duration_months, 7)

    def test_original_text_is_preserved(self):
        self.assertIn("ABC Technologies", self.entries[0].text)
        self.assertIn("React, Node.js and MongoDB", self.entries[0].text)

    def test_tech_stack_comes_from_the_entry_text(self):
        self.assertEqual(self.entries[0].technologies, ["React", "Node.js", "MongoDB"])

    def test_entry_without_technologies_reports_none(self):
        self.assertEqual(self.entries[1].technologies, [])

    def test_an_incidental_substring_is_not_a_mention(self):
        # The KB aliases "js" to JavaScript; "Node.js" must not claim it.
        self.assertNotIn("JavaScript", explicit_technologies("Built with Node.js", self.kb))
        self.assertIn("JavaScript", explicit_technologies("JavaScript and Node.js", self.kb))

    def test_an_undated_entry_is_kept_with_an_unavailable_duration(self):
        entries = extract_entries("Software Engineer at ABC Technologies\nBuilt web apps.\n", self.kb)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].duration_months)
        self.assertEqual(entries[0].duration, "Not available")
        self.assertIn("Built web apps.", entries[0].text)

    def test_empty_section_yields_nothing(self):
        self.assertEqual(extract_entries("", self.kb), [])
        self.assertEqual(extract_entries("   \n  ", self.kb), [])


class TotalExperienceTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_kb()

    def test_separate_periods_add_up(self):
        _, total = extract_experience(TWO_ROLES, self.kb)
        self.assertEqual(total, 39)  # 32 + 7, no overlap
        self.assertEqual(format_duration(total), "3 years 3 months")

    def test_overlapping_periods_are_not_double_counted(self):
        overlapping = (
            "Engineer, A Technologies\nJan 2024 - Dec 2024\n"
            "Consultant, B Solutions\nJul 2024 - Jun 2025\n"
        )
        entries = extract_entries(overlapping, self.kb)
        self.assertEqual([e.duration_months for e in entries], [12, 12])
        # Jan 2024 - Jun 2025 inclusive is 18 months, not 24.
        self.assertEqual(total_experience_months(entries), 18)

    def test_a_period_inside_another_adds_nothing(self):
        nested = (
            "Engineer, A Technologies\nJan 2023 - Dec 2025\n"
            "Advisor, B Solutions\nJan 2024 - Dec 2024\n"
        )
        self.assertEqual(total_experience_months(extract_entries(nested, self.kb)), 36)

    def test_adjacent_periods_form_one_stretch(self):
        adjacent = (
            "Engineer, A Technologies\nJan 2024 - Mar 2024\n"
            "Engineer, B Solutions\nApr 2024 - Jun 2024\n"
        )
        self.assertEqual(total_experience_months(extract_entries(adjacent, self.kb)), 6)

    def test_undated_entries_leave_the_total_unavailable(self):
        entries = extract_entries("Software Engineer at ABC Technologies\nBuilt web apps.\n", self.kb)
        self.assertIsNone(total_experience_months(entries))
        self.assertEqual(format_duration(total_experience_months(entries)), "Not available")

    def test_dated_and_undated_entries_mix_safely(self):
        mixed = (
            "Engineer, A Technologies\nJan 2024 - Dec 2024\n"
            "Volunteer, B Foundation\nHelped out.\n"
        )
        entries = extract_entries(mixed, self.kb)
        self.assertEqual(total_experience_months(entries), 12)


class SectionIsolationTests(unittest.TestCase):
    """Skills and Projects technologies must never leak into Experience."""

    def setUp(self):
        self.kb = load_kb()
        self.resume = parse_resume(FULL_RESUME)

    def test_skills_and_project_technologies_are_not_copied_in(self):
        entries = extract_entries(self.resume.experience, self.kb)
        attributed = {tech for entry in entries for tech in entry.technologies}
        # Present in Skills and Projects, absent from every experience entry.
        for leaked in ("Django", "PostgreSQL", "Kubernetes"):
            self.assertNotIn(leaked, attributed, f"{leaked} leaked into Experience")
        self.assertIn("React", attributed)

    def test_one_entry_does_not_inherit_another_entrys_stack(self):
        entries = extract_entries(self.resume.experience, self.kb)
        self.assertEqual(entries[1].technologies, [])


class ScoringIsolationTests(unittest.TestCase):
    """Experience extraction must not move the score or the recommendation."""

    def setUp(self):
        self.kb = load_kb()
        self.jd = JDSpec(required=["Python"], search_modes={"Python": "skills"})

    def test_pipeline_attaches_experience_without_changing_the_outcome(self):
        candidate = Candidate(name="Ada")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
            download = DownloadResult(candidate, Path(pdf.name), ok=True)
            with patch("resume_shortlisting.pipeline.extract_text", return_value=FULL_RESUME), \
                 patch("resume_shortlisting.pipeline.extract_link_urls", return_value=[]):
                result = process_candidate(
                    candidate, download, self.jd, self.kb, config.DEFAULT_SETTINGS,
                    check_github=False,
                )

        resume = parse_resume(FULL_RESUME)
        baseline = score_candidate(
            candidate, resume, self.jd, match_resume(resume, self.jd, self.kb),
            GithubStatus.NONE, resume.github_urls, github_checked=False,
        )
        self.assertEqual(result.score, baseline.score)
        self.assertEqual(result.recommendation, baseline.recommendation)
        self.assertEqual(result.score_breakdown, baseline.score_breakdown)
        # ...and the evidence is present all the same.
        self.assertEqual(len(result.experience_entries), 2)
        self.assertEqual(result.total_experience, "3 years 3 months")
        self.assertEqual(result.total_experience_months, 39)

    def test_extraction_failure_never_fails_a_candidate(self):
        candidate = Candidate(name="Ada")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
            download = DownloadResult(candidate, Path(pdf.name), ok=True)
            with patch("resume_shortlisting.pipeline.extract_text", return_value=FULL_RESUME), \
                 patch("resume_shortlisting.pipeline.extract_link_urls", return_value=[]), \
                 patch("resume_shortlisting.pipeline.extract_experience",
                       side_effect=RuntimeError("bad section")):
                result = process_candidate(
                    candidate, download, self.jd, self.kb, config.DEFAULT_SETTINGS,
                    check_github=False,
                )
        self.assertEqual(result.processing_status, "Analyzed")
        self.assertEqual(result.experience_entries, [])
        self.assertEqual(result.total_experience, "")

    def test_scorer_never_reads_experience_entries(self):
        resume = parse_resume(FULL_RESUME)
        report = match_resume(resume, self.jd, self.kb)
        plain = score_candidate(Candidate(name="Ada"), resume, self.jd, report,
                                GithubStatus.NONE, resume.github_urls, github_checked=False)
        # Attaching evidence after the fact cannot retroactively change a score.
        entries, months = extract_experience(resume.experience, self.kb)
        plain.experience_entries = entries
        plain.total_experience_months = months
        self.assertEqual(plain.score, plain.score_breakdown["required_keywords"]
                         + plain.score_breakdown["preferred_keywords"]
                         + plain.score_breakdown["project_evidence"]
                         + plain.score_breakdown["linkedin"])


if __name__ == "__main__":
    unittest.main()


class ResultsTableTests(unittest.TestCase):
    """The four experience columns appended to the full results table."""

    def _result(self, entries, total=""):
        from resume_shortlisting.models import ScoreResult
        return ScoreResult(
            candidate=Candidate(name="Ada"), score=70, recommendation="Shortlist",
            experience_entries=entries, total_experience=total,
        )

    def _frame(self, results):
        from resume_shortlisting.excel_writer import to_dataframe
        return to_dataframe(results)

    def setUp(self):
        from resume_shortlisting.models import ExperienceEntry
        self.entries = [
            ExperienceEntry(company="ABC Technologies", role="Research Intern",
                            technologies=["Python", "FastAPI", "OpenCV"]),
            ExperienceEntry(company="XYZ Solutions", role="Backend Developer",
                            technologies=[]),
        ]

    def test_only_the_four_columns_are_added_and_existing_ones_survive(self):
        frame = self._frame([self._result(self.entries, "2 years")])
        self.assertEqual(list(frame.columns)[-4:],
                         ["Total Experience", "Company Name", "Role", "Tech Stack"])
        for existing in ("Student Name", "Matched Skills", "Matching Score",
                         "Recommendation", "Processing Status", "Remarks"):
            self.assertIn(existing, frame.columns)
        # No duration / evidence / description leaked in.
        for unwanted in ("Duration", "Evidence", "Experience Text", "Responsibilities"):
            self.assertNotIn(unwanted, frame.columns)

    def test_every_experience_is_preserved_and_stays_aligned(self):
        frame = self._frame([self._result(self.entries, "2 years")])
        self.assertEqual(frame.loc[0, "Total Experience"], "2 years")
        self.assertEqual(frame.loc[0, "Company Name"].split("\n"),
                         ["ABC Technologies", "XYZ Solutions"])
        self.assertEqual(frame.loc[0, "Role"].split("\n"),
                         ["Research Intern", "Backend Developer"])
        self.assertEqual(frame.loc[0, "Tech Stack"].split("\n"),
                         ["Python, FastAPI, OpenCV", "Not Mentioned"])

    def test_a_candidate_without_experience_gets_empty_cells(self):
        frame = self._frame([self._result([], "")])
        for column in ("Total Experience", "Company Name", "Role", "Tech Stack"):
            self.assertEqual(frame.loc[0, column], "")

    def test_table_tech_stack_never_borrows_from_skills_or_projects(self):
        resume = parse_resume(FULL_RESUME)
        entries, months = extract_experience(resume.experience, load_kb())
        frame = self._frame([self._result(entries, format_duration(months))])
        stacks = frame.loc[0, "Tech Stack"]
        for leaked in ("Django", "PostgreSQL", "Kubernetes"):
            self.assertNotIn(leaked, stacks)
        self.assertIn("React", stacks)
        self.assertIn("Not Mentioned", stacks)


# A PDF flattens a two-column entry into a stack: company, role, location,
# dates. Reading only the line above the dates picks up the location.
STACKED = (
    "Tektronix\n"
    "Intern\n"
    "Bengaluru\n"
    "Apr 2026 - Present\n"
    "\u2013 Worked with SQL and PL/SQL on Oracle EBS to analyze data.\n"
    "Commonwealth Bank of Australia\n"
    "SDE Trainee (Apprenticeship)\n"
    "Remote\n"
    "Jan 2026 - Present\n"
    "\u2013 Built solutions using Python, JavaScript, React.js, Node.js and MongoDB.\n"
)


class StackedHeaderTests(unittest.TestCase):
    """Two-column layouts flattened by PDF extraction."""

    def setUp(self):
        self.kb = load_kb()
        self.entries = extract_entries(STACKED, self.kb)

    def test_both_roles_are_found(self):
        self.assertEqual(len(self.entries), 2)

    def test_the_employer_is_read_not_the_location(self):
        self.assertEqual([e.company for e in self.entries],
                         ["Tektronix", "Commonwealth Bank of Australia"])
        for entry in self.entries:
            self.assertNotIn(entry.company.casefold(), {"bengaluru", "remote"})

    def test_the_role_line_above_the_location_is_found(self):
        self.assertEqual([e.role for e in self.entries], ["Intern", "SDE Trainee"])

    def test_each_entry_keeps_only_its_own_technologies(self):
        self.assertEqual(self.entries[0].technologies, ["SQL", "Oracle"])
        self.assertNotIn("MongoDB", self.entries[0].technologies)
        self.assertIn("MongoDB", self.entries[1].technologies)

    def test_dash_led_bullets_do_not_become_headers(self):
        # The en-dash bullets belong to the entry body, not the next header.
        for entry in self.entries:
            self.assertNotIn("Worked with SQL", entry.company)
            self.assertNotIn("Built solutions", entry.company)

    def test_wrapped_bullet_prose_is_not_mistaken_for_a_company(self):
        wrapped = (
            "\u2013 Used Python, NLP and\n"
            "scikit-learn for real-world data analysis across many datasets\n"
            "Python Development Intern\n"
            "Jun 2023 - Aug 2023\n"
        )
        entry = extract_entries(wrapped, self.kb)[0]
        self.assertNotIn("scikit-learn for real-world", entry.company)
        self.assertEqual(entry.role, "Python Development Intern")

    def test_an_unidentifiable_role_stays_blank_rather_than_guessed(self):
        entry = extract_entries("LFX Mentorship Program\nCNCF\nMar 2025 - May 2025\n", self.kb)[0]
        self.assertEqual(entry.role, "")
        self.assertEqual(entry.company, "LFX Mentorship Program")
