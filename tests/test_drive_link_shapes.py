"""Drive link shapes students actually paste into the sheet.

No network: the folder page and the file downloads are stubbed, so these assert
our URL handling rather than Google's availability.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume_shortlisting import config
from resume_shortlisting.downloader import (
    _candidate_urls,
    _looks_like_pdf,
    _looks_like_resume,
    download_resume,
)
from resume_shortlisting.models import Candidate

FOLDER = "https://drive.google.com/drive/folders/FOLDERID123456789012?usp=drive_link"
FOLDER_HTML = (
    '<div data-id="FOLDERID123456789012"></div>'
    '<div data-id="FILEID1234567890123456" aria-label="sharan_resume.pdf PDF Shared"></div>'
    '<div data-id="FILEID1234567890123456"></div>'
)


class _Page:
    """An HTML page response, as ``requests`` would return one."""

    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


class _Payload:
    """A streamed file response, as ``requests`` would return one."""

    def __init__(self, body, content_type):
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self.body


class _TempResumesDir:
    """Redirect the resume cache so tests never touch the real one."""

    def __enter__(self):
        self._directory = tempfile.TemporaryDirectory()
        self._original = config.RESUMES_DIR
        config.RESUMES_DIR = Path(self._directory.name)
        return self

    def __exit__(self, *exc):
        config.RESUMES_DIR = self._original
        self._directory.cleanup()


class CandidateUrlTests(unittest.TestCase):
    def test_a_file_link_becomes_a_direct_download(self):
        urls = _candidate_urls("https://drive.google.com/file/d/ABC123/view", 10)
        self.assertEqual(len(urls), 1)
        self.assertIn("drive.usercontent.google.com/download?id=ABC123", urls[0])

    def test_an_open_id_link_becomes_a_direct_download(self):
        self.assertIn("id=ABC123", _candidate_urls("https://drive.google.com/open?id=ABC123", 10)[0])

    def test_a_google_doc_is_exported_as_pdf(self):
        self.assertEqual(
            _candidate_urls("https://docs.google.com/document/d/DOC123/edit", 10),
            ["https://docs.google.com/document/d/DOC123/export?format=pdf"],
        )

    def test_google_slides_are_exported_as_pdf(self):
        self.assertEqual(
            _candidate_urls("https://docs.google.com/presentation/d/S1/edit#slide=1", 10),
            ["https://docs.google.com/presentation/d/S1/export?format=pdf"],
        )

    def test_a_plain_url_is_left_alone(self):
        self.assertEqual(_candidate_urls("https://example.com/cv.pdf", 10),
                         ["https://example.com/cv.pdf"])

    def test_a_folder_resolves_to_the_files_inside_it(self):
        with patch("resume_shortlisting.downloader.requests.get", return_value=_Page(FOLDER_HTML)):
            urls = _candidate_urls(FOLDER, 10)
        # The folder's own id is not a target, and repeated ids de-duplicate.
        self.assertEqual(len(urls), 1)
        self.assertIn("id=FILEID1234567890123456", urls[0])
        self.assertNotIn("FOLDERID", urls[0])

    def test_an_unreadable_folder_yields_no_targets(self):
        with patch("resume_shortlisting.downloader.requests.get", side_effect=RuntimeError("private")):
            self.assertEqual(_candidate_urls(FOLDER, 10), [])

    def test_a_folder_never_falls_back_to_its_own_html_page(self):
        with patch("resume_shortlisting.downloader.requests.get", return_value=_Page("<div></div>")):
            self.assertEqual(_candidate_urls(FOLDER, 10), [])


class FolderDownloadTests(unittest.TestCase):
    def test_a_folder_link_downloads_the_resume_inside(self):
        requested: list[str] = []

        def fake_get(url, **kwargs):
            requested.append(url)
            if "drive/folders" in url:
                return _Page(FOLDER_HTML)
            return _Payload(b"%PDF-1.7 a real resume", "application/pdf")

        with _TempResumesDir(), patch(
            "resume_shortlisting.downloader.requests.get", side_effect=fake_get
        ):
            result = download_resume(
                Candidate(name="Folder Student", resume_url=FOLDER), config.DEFAULT_SETTINGS
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.path.read_bytes(), b"%PDF-1.7 a real resume")

        self.assertIn("drive/folders", requested[0])
        self.assertIn("id=FILEID1234567890123456", requested[1])

    def test_a_folder_whose_first_item_is_not_a_resume_falls_through(self):
        html = (
            '<div data-id="FOLDERID123456789012"></div>'
            '<div data-id="NOTARESUME1234567890AB"></div>'
            '<div data-id="FILEID1234567890123456"></div>'
        )

        def fake_get(url, **kwargs):
            if "drive/folders" in url:
                return _Page(html)
            if "NOTARESUME" in url:
                return _Payload(b"\x89PNG\r\n screenshot", "image/png")
            return _Payload(b"%PDF-1.7 the actual resume", "application/pdf")

        with _TempResumesDir(), patch(
            "resume_shortlisting.downloader.requests.get", side_effect=fake_get
        ):
            result = download_resume(
                Candidate(name="Mixed Folder", resume_url=FOLDER), config.DEFAULT_SETTINGS
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.path.read_bytes(), b"%PDF-1.7 the actual resume")

    def test_an_empty_folder_reports_a_clear_reason(self):
        with _TempResumesDir(), patch(
            "resume_shortlisting.downloader.requests.get", return_value=_Page("<div></div>")
        ):
            result = download_resume(
                Candidate(name="Empty Folder", resume_url=FOLDER), config.DEFAULT_SETTINGS
            )
        self.assertFalse(result.ok)
        self.assertIn("folder", result.error.lower())


class PayloadValidationTests(unittest.TestCase):
    """A content-type alone must not make a payload a resume."""

    def test_a_real_pdf_is_accepted(self):
        self.assertTrue(_looks_like_pdf(b"%PDF-1.7 body", "application/octet-stream"))

    def test_an_image_labelled_as_pdf_is_rejected(self):
        self.assertFalse(_looks_like_pdf(b"\xff\xd8\xff\xe0" + b"x" * 64, "application/pdf"))
        self.assertFalse(_looks_like_pdf(b"\x89PNG\r\n" + b"x" * 64, "application/pdf"))

    def test_an_html_page_labelled_as_pdf_is_rejected(self):
        self.assertFalse(_looks_like_pdf(b"<html><body>Sign in</body></html>", "application/pdf"))

    def test_docx_and_doc_magic_still_accepted(self):
        self.assertTrue(_looks_like_resume(b"PK\x03\x04", "", ".docx"))
        self.assertTrue(_looks_like_resume(bytes.fromhex("D0CF11E0A1B11AE1"), "", ".doc"))


if __name__ == "__main__":
    unittest.main()
