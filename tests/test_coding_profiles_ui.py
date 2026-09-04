"""Streamlit rendering regressions for the coding-profile evidence.

Two things are deliberately controlled here:

* External platform requests are always mocked — these tests never touch the
  live LeetCode / Codeforces / CodeChef services.
* Profile *discovery* is stubbed rather than relying on whichever candidates
  happen to be in the cached workbook. The app rewrites that workbook on every
  real run, so asserting on its contents would make these tests flaky. The
  workbook is used only as a valid candidate source; everything downstream of
  discovery (model, detail view, results table, scoring) is exercised for real.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from resume_shortlisting import coding_profiles

WORKBOOK = Path("resume_shortlisting/outputs/final_shortlisted.xlsx")
SKIP_STATS_LABEL = "Skip coding-profile stats (faster)"

# Solved/rating pairs returned instead of a real network call.
FAKE_STATS = {"leetcode": (342, 1636), "codeforces": (187, 1248), "codechef": (96, 1450)}
HANDLES = {"leetcode": "ada_lc", "codeforces": "ada_cf", "codechef": "ada_cc"}


def _all_platforms(text: str) -> dict[str, str]:
    """Pretend every candidate published all three profiles."""
    return dict(HANDLES)


def _no_platforms(text: str) -> dict[str, str]:
    """Pretend no candidate published any profile."""
    return {}


CODING_COLUMNS = [
    "LeetCode Solved", "LeetCode Profile",
    "Codeforces Solved", "Codeforces Profile",
    "CodeChef Solved", "CodeChef Profile",
]


class CodingProfileUITests(unittest.TestCase):
    #: Workbook bytes captured before any run rewrites the file on disk.
    workbook_bytes = None

    @classmethod
    def setUpClass(cls):
        if WORKBOOK.is_file():
            cls.workbook_bytes = WORKBOOK.read_bytes()

    def setUp(self):
        if not self.workbook_bytes:
            self.skipTest("cached candidate workbook is unavailable")
        coding_profiles.clear_stats_cache()
        self.addCleanup(coding_profiles.clear_stats_cache)
        self.fetched = []

    def _fetcher(self, platform):
        stats = FAKE_STATS.get(platform)
        if stats is None:
            return None

        def fetch(sess, handle):
            self.fetched.append((platform, handle))
            return stats

        return fetch

    def _run(self, *, skip_stats=True, handles=_all_platforms, fetcher=None):
        """Drive the app to a completed run and return the AppTest."""
        app = AppTest.from_file("app.py", default_timeout=300).run()
        app.radio[1].set_value("Upload Excel / CSV").run()
        app.file_uploader[0].upload(WORKBOOK.name, self.workbook_bytes).run()
        next(f for f in app.text_input
             if f.label == "Add custom required keywords (comma-separated)").set_value("Python").run()
        # Set the control explicitly rather than relying on its default.
        next(c for c in app.checkbox if c.label == SKIP_STATS_LABEL).set_value(skip_stats).run()
        active = [
            patch.object(coding_profiles, "_fetcher", fetcher or self._fetcher),
            patch.object(coding_profiles, "find_profile_handles", handles),
        ]
        for item in active:
            item.start()
            self.addCleanup(item.stop)
        app.button[0].click().run(timeout=300)
        self.assertEqual(app.exception, [])
        return app

    @staticmethod
    def _table(app):
        return app.dataframe[len(app.dataframe) - 1].value

    # ---------------------------------------------------------------- #
    # Existing surfaces keep rendering
    # ---------------------------------------------------------------- #
    def test_match_matrix_candidate_details_and_results_table_render(self):
        app = self._run()
        matrix = app.dataframe[0].value
        self.assertIn("Candidate", matrix.columns)
        self.assertIn("Score", matrix.columns)
        self.assertIn("GitHub", matrix.columns)
        # Coding evidence lives in the results table, not the keyword grid.
        self.assertNotIn("Coding", matrix.columns)

        self.assertTrue(any(e.label == "🧑‍💻 Candidate details" for e in app.expander))
        self.assertTrue(any("Required keyword evidence" in m.value for m in app.markdown))

        table = self._table(app)
        self.assertIn("Matching Score", table.columns)
        self.assertIn("Recommendation", table.columns)
        for column in CODING_COLUMNS:
            self.assertIn(column, table.columns)

    # ---------------------------------------------------------------- #
    # Profile URLs and solved counts
    # ---------------------------------------------------------------- #
    def _populated(self, table, column):
        """Non-empty cells of a coding column (unprocessed rows stay blank)."""
        values = [value for value in table[column] if value]
        self.assertTrue(values, f"{column} had no populated cell")
        return values

    def test_profile_urls_reach_the_results_table_for_every_platform(self):
        table = self._table(self._run())
        for column, prefix in (
            ("LeetCode Profile", "https://leetcode.com/u/"),
            ("Codeforces Profile", "https://codeforces.com/profile/"),
            ("CodeChef Profile", "https://www.codechef.com/users/"),
        ):
            self.assertTrue(
                all(url.startswith(prefix) for url in self._populated(table, column)),
                f"{column} carried an unexpected URL",
            )

    def test_solved_counts_render_in_the_table_and_the_detail_view(self):
        app = self._run(skip_stats=False)
        table = self._table(app)
        self.assertEqual(set(self._populated(table, "LeetCode Solved")), {"342"})
        self.assertEqual(set(self._populated(table, "Codeforces Solved")), {"187"})
        self.assertEqual(set(self._populated(table, "CodeChef Solved")), {"96"})

        markdown = [m.value for m in app.markdown]
        self.assertTrue(any("**LeetCode**" in m and "Solved: **342**" in m for m in markdown))
        self.assertTrue(any("**Codeforces**" in m and "Solved: **187**" in m for m in markdown))
        self.assertTrue(any("**CodeChef**" in m and "Solved: **96**" in m for m in markdown))
        self.assertTrue(any("[🔗 Profile](https://leetcode.com/u/" in m for m in markdown))

    def test_missing_profiles_leave_empty_cells_not_fake_data(self):
        app = self._run(handles=_no_platforms)
        table = self._table(app)
        for column in CODING_COLUMNS:
            self.assertEqual(set(table[column]), {""}, f"{column} fabricated a value")
        self.assertTrue(any("No public coding profile was found" in c.value for c in app.caption))

    # ---------------------------------------------------------------- #
    # Failure and performance controls
    # ---------------------------------------------------------------- #
    def test_failed_statistics_render_as_unavailable_without_failing_the_run(self):
        def exploding(platform):
            def fetch(sess, handle):
                raise RuntimeError("platform down")
            return fetch

        app = self._run(skip_stats=False, fetcher=exploding)
        table = self._table(app)
        self.assertEqual(set(self._populated(table, "LeetCode Solved")), {"Stats unavailable"})
        # The profile URL survives even though its statistics did not.
        self.assertTrue(all(url.startswith("https://leetcode.com/u/")
                            for url in self._populated(table, "LeetCode Profile")))
        # Candidates were still analyzed despite the statistics failure.
        self.assertTrue(any("Analyzed: " in s.value for s in app.success))

    def test_skip_stats_option_still_prevents_requests(self):
        app = self._run(skip_stats=True)
        self.assertEqual(self.fetched, [], "stats were fetched while skipping was enabled")
        table = self._table(app)
        self.assertEqual(set(self._populated(table, "LeetCode Solved")), {"Stats skipped"})
        # Detection still happened, so the link is still offered.
        self.assertTrue(all(url.startswith("https://leetcode.com/u/")
                            for url in self._populated(table, "LeetCode Profile")))

    # ---------------------------------------------------------------- #
    # Scoring isolation
    # ---------------------------------------------------------------- #
    def test_coding_evidence_does_not_change_scores_or_recommendations(self):
        with_profiles = self._run(skip_stats=False)
        with patch("resume_shortlisting.pipeline.discover_coding_profiles", return_value={}):
            baseline = AppTest.from_file("app.py", default_timeout=300).run()
            baseline.radio[1].set_value("Upload Excel / CSV").run()
            baseline.file_uploader[0].upload(WORKBOOK.name, self.workbook_bytes).run()
            next(f for f in baseline.text_input
                 if f.label == "Add custom required keywords (comma-separated)").set_value("Python").run()
            baseline.button[0].click().run(timeout=300)

        self.assertEqual(baseline.exception, [])
        self.assertEqual(
            {m.label: m.value for m in with_profiles.metric},
            {m.label: m.value for m in baseline.metric},
        )
        self.assertEqual(
            with_profiles.dataframe[0].value["Score"].tolist(),
            baseline.dataframe[0].value["Score"].tolist(),
        )
        self.assertEqual(
            self._table(with_profiles)["Recommendation"].tolist(),
            self._table(baseline)["Recommendation"].tolist(),
        )


if __name__ == "__main__":
    unittest.main()
