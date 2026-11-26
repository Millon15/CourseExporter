from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import quote

from course_exporter.archive import lesson_dir, sanitize_html, write_index, write_lesson
from course_exporter.model import Asset, Lesson, LessonRef


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name) / "archive"
        self.ref = LessonRef("getcourse", "1", "20", (2, 1), "Текст / Слайд <A>", "", ("Module & more",))

    def test_verified_files_open_relative_to_the_lesson(self):
        directory = lesson_dir(self.out, self.ref)
        self.assertEqual(directory.name, "002.001-текст-слайд-a")
        directory.mkdir(parents=True)
        files = {
            "video-1": directory / "clip #1.mp4",
            "image-1": directory / "Слайд.png",
            "file-1": directory / "guide & notes.pdf",
        }
        for path in files.values():
            path.touch()
        html = '<p>Lesson text</p><img src="asset:image-1" alt="Slide"><a href="asset:file-1">Guide</a>'
        video = Asset("video-1", "video", "Clip", "video-1")
        page = write_lesson(self.out, Lesson(self.ref, "available", html, (video,)), files)

        self.assertEqual(page, directory / "lesson.html")
        rendered = page.read_text(encoding="utf-8")
        self.assertIn("<p>Lesson text</p>", rendered)
        self.assertIn('<video controls preload="metadata" src="clip%20%231.mp4"', rendered)
        self.assertIn(f'<img src="{quote("Слайд.png")}" alt="Slide">', rendered)
        self.assertIn('<a href="guide%20%26%20notes.pdf" download>Guide</a>', rendered)
        self.assertIn('href="../index.html"', rendered)
        self.assertIn("Текст / Слайд &lt;A&gt;", rendered)

    def test_missing_and_unsafe_paths_stay_placeholders(self):
        directory = lesson_dir(self.out, self.ref)
        directory.mkdir(parents=True)
        outside = self.out.parent / "outside.pdf"
        outside.touch()
        symlink = directory / "linked.pdf"
        symlink.symlink_to(outside)
        files = {"file-1": symlink, "image-1": directory / "not-on-disk.png"}
        assets = (Asset("video-1", "video", "Clip", "video-1"),)
        html = '<a href="asset:file-1">Guide</a><img src="asset:image-1" alt="Slide">'

        rendered = write_lesson(self.out, Lesson(self.ref, "available", html, assets), files).read_text(encoding="utf-8")
        for asset_id, title in (("file-1", "Guide"), ("image-1", "Slide"), ("video-1", "Clip")):
            self.assertIn(f'data-asset="asset:{asset_id}">{title} (missing)', rendered)
        self.assertNotIn('href="asset:', rendered)
        self.assertNotIn("outside.pdf", rendered)
        self.assertNotIn("linked.pdf", rendered)

    def test_lesson_html_excludes_active_content_and_external_urls(self):
        raw = """<h2>Title</h2><p onclick="x()">Text &amp; more</p>
            <script>code()</script><form><div>Other person's reply</div><input></form>
            <iframe src="https://outside.example"></iframe><svg><text>Hidden</text></svg>
            <a href="javascript:bad()">Navigation label</a>
            <a href="https://outside.example?sig=placeholder">Reading</a>
            <img src="https://outside.example/img" srcset="https://outside.example/2x" alt="Remote slide">
            <a href="asset:file-1">Local &lt;file&gt;</a>"""
        clean = sanitize_html(raw)
        self.assertEqual(clean, sanitize_html(clean))
        for expected in ("<h2>Title</h2>", "<p>Text &amp; more</p>", "Navigation label", "Reading (external link)",
                         "Remote slide", '<a href="asset:file-1">Local &lt;file&gt;</a>'):
            self.assertIn(expected, clean)
        for forbidden in ("script", "iframe", "onclick", "bad()", "outside.example", "Other person", "Hidden"):
            self.assertNotIn(forbidden, clean)

    def test_index_includes_gated_and_undownloaded_lessons(self):
        first = LessonRef("getcourse", "1", "19", (1,), "Earlier", "")
        page = write_lesson(self.out, Lesson(self.ref, "gated", ""), {})
        index = write_index(self.out, [(self.ref, "gated"), (first, "failed & partial")])
        rendered = index.read_text(encoding="utf-8")

        self.assertEqual(index, self.out / "index.html")
        self.assertLess(rendered.index("Earlier"), rendered.index("Текст"))
        self.assertIn("Earlier <small>(failed &amp; partial)</small>", rendered)
        self.assertIn("Module &amp; more", rendered)
        self.assertIn(f'href="{quote(page.relative_to(self.out).as_posix())}"', rendered)
        self.assertIn("This lesson is gated.", page.read_text(encoding="utf-8"))

    def test_slug_cannot_escape_the_archive_and_fits_filename_limits(self):
        ref = LessonRef("getcourse", "1", "1", (1,), "../" + "я" * 500 + "/..", "")
        directory = lesson_dir(self.out, ref)
        self.assertEqual(directory.parent, self.out)
        self.assertLess(len(directory.name.encode("utf-8")), 255)

    def test_lesson_cannot_write_through_an_external_directory_symlink(self):
        self.out.mkdir()
        outside = self.out.parent / "outside"
        outside.mkdir()
        lesson_dir(self.out, self.ref).symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "outside the archive"):
            write_lesson(self.out, Lesson(self.ref, "available", ""), {})
        self.assertFalse((outside / "lesson.html").exists())
