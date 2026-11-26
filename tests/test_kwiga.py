import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from course_exporter.model import Asset, BrowserError, LessonRef, UnsupportedMedia
from course_exporter.providers.kwiga import KwigaProvider


ORIGIN = "https://school.kwiga.test"
COURSE_ID = "example-course-1"
COURSE = f"{ORIGIN}/courses/{COURSE_ID}"
LESSON = f"{COURSE}/welcome"
FIXTURES = Path(__file__).resolve().parents[2] / "peer-chat" / "course-download" / "fixtures"


class FixtureBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def evaluate(self, url, script):
        self.calls.append((url, script))
        page = self.pages[url]
        return page(script) if callable(page) else page


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class KwigaTests(unittest.TestCase):
    def setUp(self):
        self.provider = KwigaProvider()
        self.ref = LessonRef("kwiga", COURSE_ID, "welcome", (1, 1), "Welcome", LESSON)
        self.video = Asset("8", "video", "Video", "8")

    def test_discovery_uses_dom_order_and_stable_lesson_slugs(self):
        browser = FixtureBrowser({COURSE: {"entries": [
            {"url": COURSE, "title": "Course link"},
            {"url": f"{COURSE}/intro", "title": "Intro"},
            {"url": f"{COURSE}/modules/41", "title": "Welcome module"},
            {"url": f"{LESSON}/?tracking=sample", "title": "Welcome"},
            {"url": LESSON, "title": "Duplicate welcome"},
            {"url": COURSE.replace(ORIGIN, "https://outside.test") + "/other"},
            {"url": f"{COURSE}/modules"},
            {"url": f"{COURSE}/modules/41", "title": "Duplicate module"},
            {"url": f"{COURSE}/in-order", "title": "Still in the module"},
            {"url": f"{COURSE}/modules/42", "title": "Next module"},
            {"url": f"{COURSE}/last", "title": "Last lesson"},
        ]}})
        refs = self.provider.discover(browser, f"{COURSE}/?tracking=sample")
        self.assertEqual([(ref.lesson_id, ref.order, ref.modules) for ref in refs], [
            ("intro", (0, 1), ()),
            ("welcome", (1, 1), ("Welcome module",)),
            ("in-order", (1, 2), ("Welcome module",)),
            ("last", (2, 1), ("Next module",)),
        ])
        self.assertEqual(refs[1], LessonRef(
            "kwiga", COURSE_ID, "welcome", (1, 1), "Welcome", LESSON, ("Welcome module",),
        ))
        url, script = browser.calls[0]
        self.assertEqual(url, COURSE)
        self.assertIn(json.dumps(COURSE), script)
        self.assertIn("fetch(url", script)
        self.assertIn("new DOMParser()", script)
        self.assertIn("mode: 'same-origin'", script)
        self.assertNotIn("document.querySelector", script)

    def test_no_lesson_links_is_a_discovery_error(self):
        browser = FixtureBrowser({COURSE: {"entries": [{"url": COURSE}]}})
        with self.assertRaisesRegex(BrowserError, "contains no lesson links"):
            self.provider.discover(browser, COURSE)

    def test_extract_interleaves_text_and_media_without_persisting_urls(self):
        sample = "https://cdn.example.test/media.mp4?sig=fixture"
        browser = FixtureBrowser({LESSON: {"available": True, "section": {"info_units": [
            {"type_id": 3, "description_formatted": '<p onclick="bad()">Intro</p><script>bad()</script>'},
            {"type_id": 2, "files": [
                {"id": 8, "mime_type": "video/mp4", "name": "Video", "url": sample},
                {"id": 9, "mime_type": "image/png", "name": "Slide", "url": sample},
                {"id": 11, "mime_type": "image/png", "name": "Deleted", "is_deleted": True},
            ]},
            {"type_id": 3, "description": "Guide & notes", "files": [
                {"id": 8, "mime_type": "video/mp4"},
                {"id": 10, "mime_type": "application/pdf", "original_name": 'Guide "PDF"'},
            ]},
            {"type_id": 9, "description_formatted": "Quiz responses", "files": [{"id": 12}]},
        ]}}})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual(lesson.status, "available")
        self.assertEqual(lesson.assets, (
            self.video, Asset("9", "image", "Slide", "9"), Asset("10", "file", 'Guide "PDF"', "10"),
        ))
        self.assertLess(lesson.html.index("<p>Intro</p>"), lesson.html.index('src="asset:8"'))
        self.assertLess(lesson.html.index('src="asset:9"'), lesson.html.index("Guide &amp; notes"))
        self.assertIn('<a href="asset:10">Guide "PDF"</a>', lesson.html)
        self.assertIn("Kwiga info unit type 9 was not exported.", lesson.html)
        for excluded in ("bad()", sample, "Quiz responses", "Deleted"):
            self.assertNotIn(excluded, repr(lesson))

    def test_incomplete_sections_and_unknown_units_have_visible_notices(self):
        browser = FixtureBrowser({LESSON: {
            "available": True, "has_only_one_section": False,
            "section": {"info_units": [
                {"type_id": 9, "description_formatted": "A private response"},
                {"type_id": None},
                {"type_id": 3, "description_formatted": "<p>Available text</p>"},
            ]},
        }})
        lesson = self.provider.extract(browser, self.ref)
        self.assertIn("Additional Kwiga lesson sections were not exported.", lesson.html)
        self.assertIn("Kwiga info unit type 9 was not exported.", lesson.html)
        self.assertIn("Kwiga info unit type unknown was not exported.", lesson.html)
        self.assertIn("Available text", lesson.html)
        self.assertNotIn("A private response", lesson.html)

    def test_gated_lesson_has_no_assets(self):
        browser = FixtureBrowser({LESSON: {"available": False}})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual((lesson.status, lesson.assets, lesson.html), ("gated", (), ""))
        with self.assertRaisesRegex(UnsupportedMedia, "lesson is gated"):
            self.provider.resolve(browser, self.ref, self.video)

    def test_video_resolves_fresh_hls_source_even_with_mp4_extension(self):
        urls = iter(("https://cdn.example.test/media.mp4?sig=one", "https://cdn.example.test/media.mp4?sig=two"))

        def projection(script):
            self.assertIn("lesson_dto", script)
            self.assertIn("JSON.parse", script)
            self.assertTrue(script.endswith(', "8")'))
            return {"available": True, "file": {"url": next(urls)}}

        browser = FixtureBrowser({LESSON: projection})
        first = self.provider.resolve(browser, self.ref, self.video)
        second = self.provider.resolve(browser, self.ref, self.video)
        self.assertEqual((first.kind, first.headers), ("hls", {"Referer": f"{ORIGIN}/"}))
        self.assertNotEqual(first.url, second.url)
        self.assertNotIn("sig=", repr(first))
        self.assertEqual([url for url, _ in browser.calls], [LESSON, LESSON])

    def test_images_and_attachments_resolve_as_files(self):
        url = "https://cdn.example.test/download?sig=fixture"
        browser = FixtureBrowser({LESSON: {"file": {"url": url}}})
        for kind in ("image", "file"):
            with self.subTest(kind=kind):
                media = self.provider.resolve(browser, self.ref, Asset("9", kind, "Asset", "9"))
                self.assertEqual((media.kind, media.url, media.headers), ("file", url, {"Referer": f"{ORIGIN}/"}))

    def test_missing_drm_and_malformed_assets_are_unsupported(self):
        for file, reason in (
            (None, "no longer listed"),
            ({"url": "javascript:bad()"}, "URL is unavailable"),
            ({"url": "https://cdn.example.test/media?sig=fixture", "drm": True}, "DRM or encryption"),
        ):
            with self.subTest(reason=reason):
                browser = FixtureBrowser({LESSON: {"file": file}})
                with self.assertRaisesRegex(UnsupportedMedia, reason) as error:
                    self.provider.resolve(browser, self.ref, self.video)
                self.assertNotIn("sig=", str(error.exception))

    def test_unreadable_dto_is_a_retryable_browser_error(self):
        for page in ({"invalidDTO": True}, {"fetchError": True}, {"httpError": 429}):
            with self.subTest(page=page):
                browser = FixtureBrowser({LESSON: page})
                with self.assertRaises(BrowserError):
                    self.provider.resolve(browser, self.ref, self.video)

    @unittest.skipUnless(FIXTURES.is_dir(), "Jarvis's redacted workspace fixtures are unavailable")
    def test_redacted_inventory_and_lesson_fixture(self):
        course = read_fixture("kwiga-course.projection.json")
        saved_lesson = read_fixture("kwiga-lesson-1.dto-projection.json")
        entries = course["entries"]
        course_url = next(row["url"] for row in entries if row["kind"] == "course")
        browser = FixtureBrowser({course_url: course})
        refs = self.provider.discover(browser, course_url)
        self.assertEqual(len(refs), 150)
        self.assertEqual(len({ref.order[0] for ref in refs}), 17)
        self.assertEqual([ref.url for ref in refs], [row["url"] for row in entries if row["kind"] == "lesson"])
        section = saved_lesson["section"]
        slug = urlsplit(section["url"]).path.rstrip("/").rsplit("/", 1)[-1]
        ref = next(ref for ref in refs if ref.lesson_id == slug)
        self.assertEqual(ref.order, (3, 1))
        browser.pages[ref.url] = saved_lesson
        lesson = self.provider.extract(browser, ref)
        self.assertEqual(lesson.status, "available")
        video, image = next(unit["files"] for unit in section["info_units"] if unit["type_id"] == 2)
        video_id, image_id = str(video["id"]), str(image["id"])
        self.assertEqual([(asset.ref, asset.kind) for asset in lesson.assets], [
            (video_id, "video"), (image_id, "image"),
        ])
        self.assertEqual([asset.expected_seconds for asset in lesson.assets], [float(video["duration"]), None])
        self.assertIn(f'src="asset:{video_id}"', lesson.html)
        self.assertIn(f'src="asset:{image_id}"', lesson.html)
        self.assertIn("<strong>", lesson.html)
