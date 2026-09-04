import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_shortlisting import config
from resume_shortlisting.coding_profiles import (
    available_profiles,
    clear_stats_cache,
    discover_coding_profiles,
    fetch_stats,
    find_profile_handles,
    stats_summary,
    table_fields,
)
from resume_shortlisting.downloader import DownloadResult
from resume_shortlisting.keyword_matcher import match_resume
from resume_shortlisting.models import (
    CODING_PLATFORMS,
    Candidate,
    CodingProfile,
    GithubStatus,
    JDSpec,
)
from resume_shortlisting.parser import parse_resume
from resume_shortlisting.pipeline import process_candidate
from resume_shortlisting.scorer import score_candidate
from resume_shortlisting.skills_kb import load_kb

RESUME = (
    "Skills\nPython, SQL, Django\n"
    "Projects\nHospital Management System\nBuilt with Django.\n"
    "Tech Stack: Django, PostgreSQL\n"
    "GitHub: https://github.com/example/hospital\n"
    "Experience\nWorked with n8n.\n"
)

CODING_LINKS = (
    "https://leetcode.com/u/ada_l/\n"
    "https://www.codechef.com/users/ada_l\n"
    "https://codeforces.com/profile/ada_l\n"
)


class ProfileDiscoveryTests(unittest.TestCase):
    """Discovery is link-based; a bare name never associates a profile."""

    def test_leetcode_profile_is_represented(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada"), "Portfolio\nhttps://leetcode.com/u/ada_l/"
        )
        leetcode = profiles["leetcode"]
        self.assertTrue(leetcode.profile_found)
        self.assertEqual(leetcode.profile_url, "https://leetcode.com/u/ada_l/")
        self.assertEqual(leetcode.handle, "ada_l")
        self.assertEqual(leetcode.source, "resume")
        self.assertFalse(profiles["codechef"].profile_found)

    def test_codechef_profile_is_represented(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada"), "CodeChef: codechef.com/users/ada_l"
        )
        codechef = profiles["codechef"]
        self.assertTrue(codechef.profile_found)
        self.assertEqual(codechef.profile_url, "https://www.codechef.com/users/ada_l")
        self.assertIsNone(codechef.problems_solved)

    def test_codeforces_profile_is_represented(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada"), "https://codeforces.com/profile/ada_l"
        )
        codeforces = profiles["codeforces"]
        self.assertTrue(codeforces.profile_found)
        self.assertEqual(codeforces.profile_url, "https://codeforces.com/profile/ada_l")

    def test_spreadsheet_link_takes_priority_over_resume_link(self):
        candidate = Candidate(
            name="Ada",
            source_data={"LeetCode Profile": "https://leetcode.com/u/from_sheet/"},
        )
        profiles = discover_coding_profiles(candidate, "https://leetcode.com/u/from_resume/")
        self.assertEqual(profiles["leetcode"].handle, "from_sheet")
        self.assertEqual(profiles["leetcode"].source, "spreadsheet")

    def test_missing_profiles_do_not_error(self):
        profiles = discover_coding_profiles(Candidate(name="Ada"), "No links here at all.")
        self.assertEqual(set(profiles), set(CODING_PLATFORMS))
        for platform in CODING_PLATFORMS:
            self.assertFalse(profiles[platform].profile_found)
            self.assertEqual(profiles[platform].profile_url, "")
            self.assertTrue(profiles[platform].status)

    def test_name_alone_never_associates_a_profile(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada Lovelace", email="ada@example.com"), "Ada Lovelace\nDSA enthusiast"
        )
        self.assertFalse(any(p.profile_found for p in profiles.values()))

    def test_leetcode_site_pages_are_not_treated_as_profiles(self):
        handles = find_profile_handles("Solved https://leetcode.com/problems/two-sum/ daily")
        self.assertNotIn("leetcode", handles)


class MissingStatisticsTests(unittest.TestCase):
    """Unavailable public statistics must degrade, never raise."""

    def setUp(self):
        clear_stats_cache()

    def tearDown(self):
        clear_stats_cache()

    def test_unreachable_platform_yields_unavailable_status(self):
        with patch(
            "resume_shortlisting.coding_profiles._codeforces_stats",
            side_effect=RuntimeError("network down"),
        ):
            solved, rating, status = fetch_stats("codeforces", "ada_l")
        self.assertIsNone(solved)
        self.assertIsNone(rating)
        self.assertIn("unavailable", status.lower())

    def test_stats_failure_does_not_fail_candidate_processing(self):
        candidate = Candidate(name="Ada", source_data={"LeetCode": "leetcode.com/u/ada_l"})
        with tempfile.NamedTemporaryFile(suffix=".pdf") as resume:
            download = DownloadResult(candidate, Path(resume.name), ok=True)
            with patch(
                "resume_shortlisting.pipeline.extract_text",
                return_value=RESUME + CODING_LINKS,
            ), patch(
                "resume_shortlisting.coding_profiles._session",
                side_effect=RuntimeError("no network"),
            ):
                result = process_candidate(
                    candidate,
                    download,
                    JDSpec(required=["Python"], search_modes={"Python": "skills"}),
                    load_kb(),
                    config.DEFAULT_SETTINGS,
                    check_github=False,
                    fetch_coding_stats=True,
                )
        # Link discovery still succeeded; only the public statistics are missing.
        self.assertEqual(result.processing_status, "Analyzed")
        self.assertTrue(result.coding_profiles["leetcode"].profile_found)
        self.assertIsNone(result.coding_profiles["leetcode"].problems_solved)
        # One required keyword matched in Skills -> 40 -> "Consider", unchanged.
        self.assertEqual(result.score, 40.0)
        self.assertEqual(result.recommendation, "Consider")

    def test_partial_statistics_are_preserved(self):
        with patch(
            "resume_shortlisting.coding_profiles._leetcode_stats",
            return_value=(347, None),
        ):
            profiles = discover_coding_profiles(
                Candidate(name="Ada"),
                "https://leetcode.com/u/ada_l/",
                fetch_public_stats=True,
            )
        self.assertEqual(profiles["leetcode"].problems_solved, 347)
        self.assertIsNone(profiles["leetcode"].rating)


class ScoringIsolationTests(unittest.TestCase):
    """Coding evidence must not move the score or the recommendation."""

    def setUp(self):
        clear_stats_cache()
        self.addCleanup(clear_stats_cache)
        self.kb = load_kb()
        self.jd = JDSpec(
            required=["Python", "Django", "SQL"],
            search_modes={"Python": "skills", "Django": "project", "SQL": "skills_or_project"},
        )

    def _score(self, text):
        resume = parse_resume(text)
        report = match_resume(resume, self.jd, self.kb)
        return score_candidate(
            Candidate(name="Ada"), resume, self.jd, report,
            GithubStatus.NONE, [], github_checked=False,
        )

    def test_coding_profiles_do_not_change_score_or_recommendation(self):
        without = self._score(RESUME)
        with_profiles = self._score(RESUME + CODING_LINKS)
        self.assertEqual(without.score, with_profiles.score)
        self.assertEqual(without.recommendation, with_profiles.recommendation)
        self.assertEqual(without.score_breakdown, with_profiles.score_breakdown)

    def test_strong_statistics_do_not_change_score_or_recommendation(self):
        baseline = self._score(RESUME)
        candidate = Candidate(name="Ada")
        with tempfile.NamedTemporaryFile(suffix=".pdf") as resume:
            download = DownloadResult(candidate, Path(resume.name), ok=True)
            with patch(
                "resume_shortlisting.pipeline.extract_text", return_value=RESUME + CODING_LINKS
            ), patch(
                "resume_shortlisting.coding_profiles._leetcode_stats", return_value=(500, None)
            ), patch(
                "resume_shortlisting.coding_profiles._codechef_stats", return_value=(186, 1450)
            ), patch(
                "resume_shortlisting.coding_profiles._codeforces_stats", return_value=(None, 1600)
            ):
                result = process_candidate(
                    candidate, download, self.jd, self.kb, config.DEFAULT_SETTINGS,
                    check_github=False, fetch_coding_stats=True,
                )
        self.assertEqual(result.coding_profiles["leetcode"].problems_solved, 500)
        self.assertEqual(result.coding_profiles["codechef"].rating, 1450)
        self.assertEqual(result.coding_profiles["codeforces"].rating, 1600)
        self.assertEqual(result.score, baseline.score)
        self.assertEqual(result.recommendation, baseline.recommendation)
        self.assertEqual(result.score_breakdown, baseline.score_breakdown)

    def test_report_dict_carries_coding_profiles_without_touching_score(self):
        candidate = Candidate(name="Ada")
        result = self._score(RESUME + CODING_LINKS)
        result.coding_profiles = discover_coding_profiles(candidate, RESUME + CODING_LINKS)
        payload = result.to_report_dict()
        self.assertTrue(payload["coding_profiles"]["leetcode"]["profile_found"])
        self.assertEqual(payload["score"], round(result.score, 1))


class ExistingBehaviourRegressionTests(unittest.TestCase):
    """The pre-existing discovery and processing-status behaviour is intact."""

    def setUp(self):
        self.kb = load_kb()

    def test_whole_resume_discovery_still_works(self):
        resume = parse_resume(RESUME + CODING_LINKS)
        jd = JDSpec(required=["n8n"], search_modes={"n8n": "whole_resume"})
        report = match_resume(resume, jd, self.kb)
        self.assertEqual(report.matches[0].match_type, "whole_resume")
        result = score_candidate(
            Candidate(name="Ada"), resume, jd, report, GithubStatus.NONE, [], github_checked=False
        )
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.recommendation, "Reject")

    def test_experience_discovery_still_works(self):
        resume = parse_resume(RESUME + CODING_LINKS)
        jd = JDSpec(required=["n8n"], search_modes={"n8n": "experience"})
        report = match_resume(resume, jd, self.kb)
        self.assertEqual(report.matches[0].match_type, "experience")
        self.assertEqual(report.matched_in["n8n"], ["Experience"])

    def test_processing_status_behaviour_still_works(self):
        candidate = Candidate(name="Private", source_data={"LeetCode": "leetcode.com/u/ada_l"})
        result = process_candidate(
            candidate,
            DownloadResult(candidate, None, ok=False, error="HTTP 403: permission denied"),
            JDSpec(),
            self.kb,
            config.DEFAULT_SETTINGS,
            check_github=False,
        )
        self.assertEqual(result.processing_status, "Access Denied")
        self.assertIsNone(result.score)
        self.assertEqual(result.recommendation, "N/A")
        self.assertEqual(result.coding_profiles, {})


class StatsCacheTests(unittest.TestCase):
    """One request per handle, however many candidates reference it."""

    def setUp(self):
        clear_stats_cache()

    def tearDown(self):
        clear_stats_cache()

    def test_repeated_lookups_hit_the_cache(self):
        calls = []

        def fake(sess, handle):
            calls.append(handle)
            return 342, None

        with patch("resume_shortlisting.coding_profiles._fetcher", return_value=fake):
            first = fetch_stats("leetcode", "ada_l")
            second = fetch_stats("leetcode", "ADA_L")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "handle was requested more than once")

    def test_failures_are_cached_too(self):
        calls = []

        def failing(sess, handle):
            calls.append(handle)
            raise RuntimeError("platform down")

        with patch("resume_shortlisting.coding_profiles._fetcher", return_value=failing):
            fetch_stats("codechef", "ada_l")
            solved, rating, status = fetch_stats("codechef", "ada_l")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(solved)
        self.assertIsNone(rating)
        self.assertIn("unavailable", status.lower())


class DisplayFormattingTests(unittest.TestCase):
    """Wording shared by the match matrix, results table and detail view."""

    def _profile(self, platform, **kwargs):
        return CodingProfile(platform=platform, profile_found=True, **kwargs)

    def test_solved_count_is_reported_when_available(self):
        self.assertEqual(
            stats_summary(self._profile("leetcode", problems_solved=342)), "342 solved"
        )

    def test_explicit_zero_is_reported_as_zero(self):
        self.assertEqual(
            stats_summary(self._profile("codechef", problems_solved=0)), "0 solved"
        )

    def test_unknown_count_never_becomes_zero(self):
        label = stats_summary(self._profile("codeforces", status="Profile found; public stats unavailable"))
        self.assertEqual(label, "Stats unavailable")
        self.assertNotIn("0", label)

    def test_skipped_stats_are_distinguished_from_unavailable(self):
        skipped = self._profile("leetcode", status="Profile found; public stats not fetched")
        self.assertEqual(stats_summary(skipped), "Stats skipped")

    def test_rating_is_used_when_no_solved_count_is_published(self):
        self.assertEqual(
            stats_summary(self._profile("codeforces", rating=1248)), "rating 1248"
        )

    def test_missing_profile_has_no_statistics_label(self):
        self.assertEqual(stats_summary(CodingProfile(platform="leetcode")), "")

    def test_available_profiles_are_found_only_and_ordered(self):
        profiles = {
            "leetcode": self._profile("leetcode", problems_solved=342),
            "codeforces": CodingProfile(platform="codeforces"),
            "codechef": self._profile("codechef", problems_solved=96),
        }
        self.assertEqual(
            [p.platform for p in available_profiles(profiles)], ["leetcode", "codechef"]
        )

    def test_table_fields_carry_solved_count_and_url(self):
        profiles = {
            "leetcode": self._profile("leetcode", profile_url="https://leetcode.com/u/ada_l/", problems_solved=342),
            "codeforces": self._profile("codeforces", profile_url="https://codeforces.com/profile/ada_l", problems_solved=187),
            "codechef": CodingProfile(platform="codechef"),
        }
        fields = table_fields(profiles)
        self.assertEqual(fields["LeetCode Solved"], "342")
        self.assertEqual(fields["LeetCode Profile"], "https://leetcode.com/u/ada_l/")
        self.assertEqual(fields["Codeforces Solved"], "187")
        self.assertEqual(fields["Codeforces Profile"], "https://codeforces.com/profile/ada_l")
        # A platform without a profile gets empty cells, never a zero or a URL.
        self.assertEqual(fields["CodeChef Solved"], "")
        self.assertEqual(fields["CodeChef Profile"], "")

    def test_table_fields_report_why_a_count_is_missing(self):
        profiles = {
            "leetcode": self._profile(
                "leetcode", profile_url="https://leetcode.com/u/ada_l/",
                status="Profile found; public stats not fetched",
            ),
            "codeforces": self._profile(
                "codeforces", profile_url="https://codeforces.com/profile/ada_l",
                status="Profile found; public stats unavailable",
            ),
        }
        fields = table_fields(profiles)
        self.assertEqual(fields["LeetCode Solved"], "Stats skipped")
        self.assertEqual(fields["Codeforces Solved"], "Stats unavailable")
        self.assertEqual(fields["LeetCode Profile"], "https://leetcode.com/u/ada_l/")

    def test_table_fields_are_empty_without_profiles(self):
        fields = table_fields({})
        self.assertEqual(set(fields.values()), {""})

    def test_profile_urls_are_preserved_end_to_end(self):
        profiles = discover_coding_profiles(
            Candidate(name="Ada"),
            "leetcode.com/u/ada_l codeforces.com/profile/ada_cf codechef.com/users/ada_cc",
        )
        self.assertEqual(
            [p.profile_url for p in available_profiles(profiles)],
            [
                "https://leetcode.com/u/ada_l/",
                "https://codeforces.com/profile/ada_cf",
                "https://www.codechef.com/users/ada_cc",
            ],
        )


class ResultsTableTests(unittest.TestCase):
    """The full results table carries readable coding evidence."""

    def test_coding_column_is_present_and_populated(self):
        from resume_shortlisting.excel_writer import to_dataframe
        from resume_shortlisting.models import ScoreResult

        with_profiles = ScoreResult(
            candidate=Candidate(name="Ada"), score=70, recommendation="Shortlist",
            coding_profiles={
                "leetcode": CodingProfile(
                    platform="leetcode", profile_found=True,
                    profile_url="https://leetcode.com/u/ada_l/", problems_solved=342,
                ),
            },
        )
        without = ScoreResult(candidate=Candidate(name="Bob"), score=70, recommendation="Shortlist")
        frame = to_dataframe([with_profiles, without])
        for column in ("LeetCode Solved", "LeetCode Profile", "Codeforces Solved",
                       "Codeforces Profile", "CodeChef Solved", "CodeChef Profile"):
            self.assertIn(column, frame.columns)
        self.assertEqual(frame["LeetCode Solved"].tolist(), ["342", ""])
        self.assertEqual(
            frame["LeetCode Profile"].tolist(), ["https://leetcode.com/u/ada_l/", ""]
        )
        self.assertEqual(frame["CodeChef Solved"].tolist(), ["", ""])
        # Existing columns and their order are untouched.
        self.assertEqual(frame.columns[0], "Student Name")
        self.assertIn("Matching Score", frame.columns)
        self.assertIn("Recommendation", frame.columns)


if __name__ == "__main__":
    unittest.main()
