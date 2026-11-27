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
        files = {"file-1": symlink}
        assets = (Asset("video-1", "video", "Clip", "video-1"),)
        html = '<a href="asset:file-1">Guide</a><img src="asset:image-1" alt="Slide">'

        rendered = write_lesson(self.out, Lesson(self.ref, "available", html, assets), files).read_text(encoding="utf-8")
        for asset_id, title in (("file-1", "Guide"), ("image-1", "Slide"), ("video-1", "Clip")):
            self.assertIn(f'data-asset="asset:{asset_id}">{title} (missing)', rendered)
        self.assertNotIn('href="asset:', rendered)
        self.assertNotIn("outside.pdf", rendered)
        self.assertNotIn("linked.pdf", rendered)

    def test_verified_drive_only_assets_link_without_a_local_file(self):
        directory = lesson_dir(self.out, self.ref)
        files = {"link-1": directory / "Reading copy.pdf", "image-1": directory / "Slide.png"}
        html = '<a href="asset:link-1">saved copy</a><img src="asset:image-1" alt="Slide">'

        rendered = write_lesson(self.out, Lesson(self.ref, "available", html), files).read_text(encoding="utf-8")
        self.assertIn('<a href="Reading%20copy.pdf" download>saved copy</a>', rendered)
        self.assertIn('<img src="Slide.png" alt="Slide">', rendered)
        self.assertTrue(all(not path.exists() for path in files.values()))

    def test_lesson_html_keeps_outside_anchors_and_excludes_active_content(self):
        raw = """<h2>Title</h2><p onclick="x()">Text &amp; more</p>
            <script>code()</script><form><div>Other person's reply</div><input></form>
            <iframe src="https://outside.example"></iframe><svg><text>Hidden</text></svg>
            <a href="javascript:bad()">Navigation label</a>
            <a href="https://outside.example/?v=a&amp;b=&quot;quote&quot;" target="_blank">Reading</a>
            <a href="//outside.example/handout">Handout</a>
            <a href="http://outside.example/index">Index</a>
            <a href="/lessons/20">Relative</a>
            <a href="mailto:owner@example.test">Email</a>
            <a href="https:///no-host">Invalid URL</a>
            <img src="https://outside.example/img" srcset="https://outside.example/2x" alt="Remote slide">
            <a href="asset:file-1">Local &lt;file&gt;</a>"""
        clean = sanitize_html(raw)
        self.assertEqual(clean, sanitize_html(clean))
        rendered = write_lesson(self.out, Lesson(self.ref, "available", clean), {}).read_text(encoding="utf-8")
        for expected in ('<a href="https://outside.example/?v=a&amp;b=&quot;quote&quot;" rel="noreferrer">Reading</a>',
                         '<a href="https://outside.example/handout" rel="noreferrer">Handout</a>',
                         '<a href="http://outside.example/index" rel="noreferrer">Index</a>',
                         "<h2>Title</h2>", "<p>Text &amp; more</p>", "<span>Navigation label</span>",
                         "<span>Relative</span>", "<span>Email</span>", "<span>Invalid URL</span>", "Remote slide"):
            self.assertIn(expected, clean)
            self.assertIn(expected, rendered)
        self.assertIn('<a href="asset:file-1">Local &lt;file&gt;</a>', clean)
        for forbidden in ("script", "iframe", "onclick", "bad()", "Other person", "Hidden", "(external link)",
                          "outside.example/img", "target="):
            self.assertNotIn(forbidden, clean)
            self.assertNotIn(forbidden, rendered)

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
