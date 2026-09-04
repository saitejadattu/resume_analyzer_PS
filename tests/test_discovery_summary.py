from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


class DiscoverySummaryTests(unittest.TestCase):
    def test_whole_resume_count_and_matrix_are_visible(self):
        workbook = Path("resume_shortlisting/outputs/final_shortlisted.xlsx")
        if not workbook.is_file():
            self.skipTest("cached candidate workbook is unavailable")

        app = AppTest.from_file("app.py").run()
        app.radio[1].set_value("Upload Excel / CSV").run()
        app.file_uploader[0].upload(workbook.name, workbook.read_bytes()).run()
        next(field for field in app.text_input if field.label == "Add custom required keywords (comma-separated)").set_value("leetcode").run()
        next(field for field in app.selectbox if field.label == "leetcode").set_value("Whole Resume").run()
        app.button[0].click().run(timeout=180)

        self.assertEqual(app.exception, [])
        matrix = app.dataframe[0].value
        whole_resume_count = int((matrix["leetcode"] == "🔎 Whole Resume").sum())
        self.assertTrue(any(f"Whole Resume matches: {whole_resume_count}" in item.value for item in app.caption))
        if whole_resume_count:
            self.assertTrue((matrix["leetcode"] == "🔎 Whole Resume").any())


if __name__ == "__main__":
    unittest.main()
