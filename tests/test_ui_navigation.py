from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


class UINavigationTests(unittest.TestCase):
    def _run_with_two_candidates(self):
        workbook = Path("resume_shortlisting/outputs/final_shortlisted.xlsx")
        if not workbook.is_file():
            self.skipTest("cached candidate workbook is unavailable")
        app = AppTest.from_file("app.py").run()
        app.radio[1].set_value("Upload Excel / CSV").run()
        app.file_uploader[0].upload(workbook.name, workbook.read_bytes()).run()
        keyword_input = next(field for field in app.text_input if field.label == "Add custom required keywords (comma-separated)")
        keyword_input.set_value("Python").run()
        app.number_input[0].set_value(2).run()
        app.button[0].click()
        app.run(timeout=120)
        self.assertEqual(app.exception, [])
        return app

    def test_candidate_details_and_profile_downloads_are_independent(self):
        app = self._run_with_two_candidates()
        self.assertFalse(any(field.label in ("Candidate Details Filter", "Profile Download Filter") for field in app.selectbox))
        details = next(expander for expander in app.expander if expander.label == "🧑‍💻 Candidate details")
        downloads = next(expander for expander in app.expander if expander.label == "Profile downloads")
        self.assertFalse(details.proto.expanded)
        self.assertFalse(downloads.proto.expanded)

    def test_profile_download_controls_remain_available(self):
        app = self._run_with_two_candidates()
        downloads = next(expander for expander in app.expander if expander.label == "Profile downloads")
        self.assertFalse(downloads.proto.expanded)
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("st.container(height=620", source)
        self.assertIn("st.container(height=420", source)
        self.assertIn('"Select All"', source)
        self.assertIn('"Clear Selection"', source)
        self.assertIn('"Strong Shortlist"', source)
        self.assertIn('"Shortlist"', source)
        self.assertIn('"Consider"', source)
        self.assertTrue(any(button.label == "⬇️ Download Selected Profiles" for button in app.download_button))


if __name__ == "__main__":
    unittest.main()
