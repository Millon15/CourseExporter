import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch
from urllib.parse import urlsplit

from course_exporter.media import DownloadError
from course_exporter.model import Asset, BrowserError, LessonRef, UnsupportedMedia
from course_exporter.providers.getcourse import GetCourseProvider
from course_exporter.state import CourseState


ORIGIN = "https://example.test"
PLAYER = "https://vh-api-1-de.gceuproxy.com/sign-player/test?sig=placeholder"
LESSON = f"{ORIGIN}/teach/control/lesson/view/id/20"
FIXTURES = Path(__file__).resolve().parents[2] / "peer-chat" / "course-download" / "fixtures"
FETCH_PLAYER = "course_exporter.providers.getcourse.fetch_public_text"


class FixtureBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def evaluate(self, url, script):
        self.calls.append((url, script))
        projection = self.pages[url]
        if callable(projection):
            return projection(script)
        return projection


def stream(origin, stream_id):
    return f"{origin}/teach/control/stream/view/id/{stream_id}"


def read_fixture(filename):
    return json.loads((FIXTURES / filename).read_text(encoding="utf-8"))


def player_html(config):
    return f"<html><script>window.configs = {json.dumps(config)}; notExecuted()</script></html>"


class GetCourseTests(unittest.TestCase):
    def setUp(self):
        self.provider = GetCourseProvider()
        self.ref = LessonRef("getcourse", "10", "20", (1,), "Lesson", LESSON)
        self.video = Asset("video-1", "video", "Video 1", "video-1")

    def test_discovery_preserves_order_and_ignores_duplicates_cycles_and_other_hosts(self):
        root, child = stream(ORIGIN, 10), stream(ORIGIN, 11)
        browser = FixtureBrowser({
            root: {"links": [
                {"url": "/teach/control/stream/index", "text": "Courses"},
                {"url": LESSON, "text": "First lesson\nDescription"},
                {"url": "https://outside.test/teach/control/stream/view/id/12", "text": "Outside"},
                {"url": child, "text": "2 урока\nModule label"},
                {"url": "/pl/teach/control/lesson/view?id=22", "text": "Third lesson"},
            ]},
            child: {"title": "Module heading", "links": [
                {"url": root, "text": "Back to the bundle"},
                {"url": "/pl/teach/control/lesson/view?id=20", "text": "Same lesson"},
                {"url": "/pl/teach/control/lesson/view?id=21&editMode=0", "text": "Просмотрен\nSecond lesson"},
            ]},
        })
        refs = self.provider.discover(browser, root + "?ignored=yes")
        self.assertEqual([(ref.lesson_id, ref.order, ref.title, ref.modules) for ref in refs], [
            ("20", (1,), "First lesson", ()),
            ("21", (2, 1), "Second lesson", ("Module heading",)),
            ("22", (3,), "Third lesson", ()),
        ])
        self.assertEqual({ref.course_id for ref in refs}, {"10"})
        self.assertEqual(refs[1].url, f"{ORIGIN}/teach/control/lesson/view/id/21")
        self.assertEqual([url for url, _ in browser.calls], [root, child])
        for url, script in browser.calls:
            self.assertIn(json.dumps(url), script)
            self.assertIn("fetch(url", script)
            self.assertIn("new DOMParser()", script)
            self.assertIn("mode: 'same-origin'", script)
            self.assertIn("cache: 'no-store'", script)
            self.assertNotIn("document.querySelector", script)

    def test_expired_browser_session_is_an_error(self):
        browser = FixtureBrowser({LESSON: {"login": True}})
        with self.assertRaisesRegex(BrowserError, "requires sign-in"):
            self.provider.extract(browser, self.ref)

    def test_lesson_rows_include_locked_lessons_with_no_anchor(self):
        root = stream(ORIGIN, 10)

        def projection(script):
            self.assertIn('li[class*="lesson-id-"]', script)
            self.assertIn(".link.title", script)
            self.assertIn(".lesson-list__video-watched", script)
            self.assertIn("user-state-not_reached", script)
            return {"links": [
                {"lessonId": "20", "title": "Introduction", "text": "Дата и время начала\nIntroduction"},
                {"lessonId": "21", "title": "Current lesson", "gated": False},
                {"lessonId": "22", "title": "Locked lesson", "gated": True},
                {"url": "/teach/control/lesson/view/id/23", "text": "Дата и время начала\nAnother lesson"},
            ]}

        browser = FixtureBrowser({root: projection, f"{ORIGIN}/teach/control/lesson/view/id/21": {"html": "<p>Text</p>"}})
        refs = self.provider.discover(browser, root)
        self.assertEqual([(ref.lesson_id, ref.order, ref.title) for ref in refs], [
            ("20", (1,), "Introduction"), ("21", (2,), "Current lesson"),
            ("22", (3,), "Locked lesson"), ("23", (4,), "Another lesson"),
        ])
        self.assertEqual(urlsplit(refs[2].url).path, "/teach/control/lesson/view/id/22")
        self.assertEqual(self.provider.extract(browser, refs[1]).status, "available")
        self.assertEqual([url for url, _ in browser.calls], [root, f"{ORIGIN}/teach/control/lesson/view/id/21"])

        with TemporaryDirectory() as directory:
            state = CourseState(Path(directory) / "state.sqlite3")
            try:
                state.record_discovery(refs)
                saved_ref = state.lessons()[2][0]
            finally:
                state.close()
        resumed = GetCourseProvider()
        locked_browser = FixtureBrowser({})
        lesson = resumed.extract(locked_browser, saved_ref)
        self.assertEqual((lesson.status, lesson.html, lesson.assets), ("gated", "", ()))
        with self.assertRaisesRegex(UnsupportedMedia, "gated"):
            resumed.resolve(locked_browser, saved_ref, self.video)
        self.assertEqual(locked_browser.calls, [])

    def test_extracts_sanitized_html_and_opaque_asset_ids(self):
        attachment = "https://cdn.example.test/download?sig=placeholder"
        browser = FixtureBrowser({LESSON: {
            "html": '<p onclick="bad()">Text</p><script>bad()</script>'
                    '<video src="asset:video-1"></video>'
                    '<img src="asset:image-1" alt="Slide">'
                    '<a href="asset:file-1">Guide</a>',
            "frames": [{"url": PLAYER}],
            "assets": [
                {"kind": "image", "title": "Slide", "url": attachment},
                {"kind": "file", "title": "Guide", "url": attachment},
            ],
        }})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual(lesson.status, "available")
        self.assertEqual([(asset.kind, asset.ref) for asset in lesson.assets], [
            ("video", "video-1"), ("image", "image-1"), ("file", "file-1"),
        ])
        self.assertIn("<p>Text</p>", lesson.html)
        self.assertIn('src="asset:image-1"', lesson.html)
        self.assertNotIn("bad()", lesson.html)
        self.assertNotIn(attachment, repr(lesson))
        media = self.provider.resolve(browser, self.ref, lesson.assets[2])
        self.assertEqual((media.kind, media.url), ("file", attachment))

    def test_outside_links_survive_without_shifting_asset_ids(self):
        reading = "https://docs.google.com/document/d/example/edit"
        file_url = "https://cdn.example.test/fileservice/file/guide?sig=placeholder"

        def projection(script):
            self.assertNotIn("removeAttribute('href')", script)
            self.assertNotIn("(external link)", script)
            return {
                "html": '<video src="asset:video-1"></video>'
                        f'<a href="{reading}">Reading</a>'
                        '<img src="asset:image-1" alt="Slide">'
                        '<a href="/lessons/20">Next lesson</a>'
                        '<a href="asset:file-1">Guide</a>'
                        '<video src="asset:video-direct-1"></video>'
                        '<a href="asset:file-2">Workbook</a>',
                "frames": [{"url": PLAYER}],
                "videos": [{"title": "Direct video", "sources": []}],
                "assets": [
                    {"kind": "image", "title": "Slide", "url": "https://cdn.example.test/slide.png"},
                    {"kind": "file", "title": "Guide", "url": file_url},
                    {"kind": "file", "title": "Workbook", "url": "https://cdn.example.test/download"},
                ],
            }

        browser = FixtureBrowser({LESSON: projection})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual([asset.asset_id for asset in lesson.assets],
                         ["video-1", "video-direct-1", "image-1", "file-1", "file-2"])
        self.assertIn(f'<a href="{reading}" rel="noreferrer">Reading</a>', lesson.html)
        self.assertIn('<span>Next lesson</span>', lesson.html)
        self.assertIn('<a href="asset:file-1">Guide</a>', lesson.html)
        self.assertNotIn(file_url, lesson.html)
        media = self.provider.resolve(browser, self.ref, lesson.assets[3])
        self.assertEqual((media.kind, media.url), ("file", file_url))

    def test_homework_task_text_is_exported_without_answers_or_the_send_form(self):
        from course_exporter.providers.getcourse import _LESSON
        self.assertIn("const mission = '.lesson-mission-text';", _LESSON)
        for answer in (".self-answers", ".other-answers", ".answer_wrapper", ".lesson-send-answer-block"):
            self.assertIn(answer, _LESSON.split("const answers = ", 1)[1].split(";", 1)[0])
        browser = FixtureBrowser({LESSON: {"html": "<p>Lesson</p>\n<h3>Задание</h3><p>Task</p>"}})
        self.assertIn("<h3>Задание</h3><p>Task</p>", self.provider.extract(browser, self.ref).html)

    def test_embedded_players_get_their_own_ids_and_resolve_to_their_stream(self):
        kinescope = "https://kinescope.io/embed/vhvG73Gj6wxtkNfdYm9U6z"
        youtube = "https://www.youtube.com/embed/abcdefghijk"
        master = "https://kinescope.io/2e6f05fa-fb96-4edd-a8bd-8ea381a88442/master.m3u8"
        browser = FixtureBrowser({LESSON: {
            "html": '<video src="asset:video-1"></video><video src="asset:video-embed-1"></video>'
                    '<video src="asset:video-embed-2"></video>',
            "frames": [{"url": PLAYER}],
            "embeds": [{"title": "Эфир", "url": kinescope}, {"title": "Clip", "url": youtube}],
        }})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual([asset.asset_id for asset in lesson.assets], ["video-1", "video-embed-1", "video-embed-2"])
        page = f'<script>var p = {{"duration":4827.6,"hls":{{"src":"{master}"}},"drm":null}};</script>'
        with patch(FETCH_PLAYER, return_value=page) as fetched:
            media = self.provider.resolve(browser, self.ref, lesson.assets[1])
        self.assertEqual(fetched.call_args.args[0], kinescope)
        self.assertEqual((media.kind, media.url, media.headers, media.expected_seconds),
                         ("hls", master, {"Referer": "https://kinescope.io/"}, 4827.6))
        media = self.provider.resolve(browser, self.ref, lesson.assets[2])
        self.assertEqual((media.kind, media.url), ("hls", youtube))

    def test_a_kinescope_player_with_drm_or_without_a_playlist_is_unsupported(self):
        browser = FixtureBrowser({LESSON: {"html": "", "embeds": [{"url": "https://kinescope.io/embed/abc"}]}})
        asset = self.provider.extract(browser, self.ref).assets[0]
        for page, reason in (('{"drm":{"widevine":{"licenseUrl":"x"}}}', "DRM"), ("<html></html>", "no HLS")):
            with self.subTest(reason=reason), patch(FETCH_PLAYER, return_value=page):
                with self.assertRaisesRegex(UnsupportedMedia, reason):
                    self.provider.resolve(browser, self.ref, asset)

    def test_gated_lesson_has_no_assets(self):
        browser = FixtureBrowser({LESSON: {"gated": True, "frames": [{"url": PLAYER}]}})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual((lesson.status, lesson.html, lesson.assets), ("gated", "", ()))
        with self.assertRaisesRegex(UnsupportedMedia, "gated"):
            self.provider.resolve(browser, self.ref, self.video)

    def test_video_roots_use_projected_frame_order_and_resolve_the_video_url(self):
        second = "https://vh-api-1-de.gceuproxy.com/sign-player/second?sig=fixture"

        def projection(script):
            self.assertIn("page.querySelectorAll('.vhi-root, iframe')", script)
            self.assertIn("data-iframe-src", script)
            return {
                "html": '<video src="asset:video-1"></video><video src="asset:video-2"></video>',
                "frames": [{"title": "First", "url": PLAYER}, {"title": "Second", "url": second}],
            }

        browser = FixtureBrowser({LESSON: projection})
        lesson = self.provider.extract(browser, self.ref)
        self.assertEqual([(asset.ref, asset.title) for asset in lesson.assets], [
            ("video-1", "First"), ("video-2", "Second"),
        ])
        self.assertIn('src="asset:video-2"', lesson.html)
        html = player_html({"masterPlaylistUrl": "https://cdn.example.test/video.m3u8"})
        with patch(FETCH_PLAYER, return_value=html) as fetch:
            self.provider.resolve(browser, self.ref, lesson.assets[1])
        fetch.assert_called_once_with(second, {"Referer": f"{ORIGIN}/"})
        self.assertTrue(all("sig=" not in script for _, script in browser.calls))

    def test_signed_player_is_resolved_again_for_each_attempt(self):
        pages = [
            player_html({"media": [{"masterPlaylistUrl": "../watch.mp4?sig=one", "videoDuration": "776.32"}],
                         "description": 'Some } and " inside the JSON', "drm": "none"}),
            player_html({"masterPlaylistUrl": "https://cdn.example.test/watch.mp4?sig=two",
                         "videoDuration": 776.32}),
        ]
        browser = FixtureBrowser({LESSON: {"frames": [{"url": PLAYER}]}})
        with patch(FETCH_PLAYER, side_effect=pages) as fetch:
            first = self.provider.resolve(browser, self.ref, self.video)
            second = self.provider.resolve(browser, self.ref, self.video)
        self.assertEqual(fetch.call_args_list, [call(PLAYER, {"Referer": f"{ORIGIN}/"})] * 2)
        self.assertEqual(first.kind, "hls")
        self.assertEqual(first.url, "https://vh-api-1-de.gceuproxy.com/watch.mp4?sig=one")
        self.assertNotEqual(first.url, second.url)
        self.assertEqual(first.expected_seconds, 776.32)
        self.assertEqual(second.expected_seconds, 776.32)
        self.assertNotIn("sig=", repr(first))
        self.assertEqual(first.headers, {"Referer": "https://vh-api-1-de.gceuproxy.com/"})
        self.assertEqual([url for url, _ in browser.calls], [LESSON, LESSON])
        self.assertTrue(all("sig=" not in script for _, script in browser.calls))

    def test_explicit_drm_and_unusable_player_config_are_unsupported(self):
        cases = [
            (player_html({"media": [{"drm": {"keySystem": "com.widevine.alpha"}}],
                          "masterPlaylistUrl": "https://cdn.example.test/master.m3u8"}), "DRM or encryption"),
            (player_html({"masterPlaylistUrl": "javascript:alert(1)",
                          "mp4Url": "http://cdn.example.test/video.mp4"}), "no supported source"),
            ("<script>window.configs = buildPlayer();</script>", "does not expose a readable configuration"),
            ('<script src="player.js">window.configs = {};</script>', "does not expose a readable configuration"),
        ]
        for html, reason in cases:
            with self.subTest(reason=reason):
                browser = FixtureBrowser({LESSON: {"frames": [{"url": PLAYER}]}})
                with patch(FETCH_PLAYER, return_value=html), self.assertRaisesRegex(UnsupportedMedia, reason) as error:
                    self.provider.resolve(browser, self.ref, self.video)
                self.assertNotIn(PLAYER, str(error.exception))

    def test_player_http_403_is_unsupported_and_transient_error_can_retry(self):
        browser = FixtureBrowser({LESSON: {"frames": [{"url": PLAYER}]}})
        forbidden = DownloadError("page fetch: HTTPError: HTTP Error 403: Forbidden")
        with patch(FETCH_PLAYER, side_effect=forbidden), self.assertRaisesRegex(UnsupportedMedia, "HTTP 403") as error:
            self.provider.resolve(browser, self.ref, self.video)
        self.assertNotIn(PLAYER, str(error.exception))
        unavailable = DownloadError("page fetch: HTTPError: HTTP Error 503: Unavailable")
        with patch(FETCH_PLAYER, side_effect=unavailable), self.assertRaisesRegex(DownloadError, "HTTP Error 503"):
            self.provider.resolve(browser, self.ref, self.video)

    def test_player_can_offer_an_mp4_without_a_usable_duration(self):
        browser = FixtureBrowser({LESSON: {"frames": [{"url": PLAYER}]}})
        html = player_html({"mp4Url": "https://cdn.example.test/video.mp4", "videoDuration": "NaN"})
        with patch(FETCH_PLAYER, return_value=html):
            media = self.provider.resolve(browser, self.ref, self.video)
        self.assertEqual((media.kind, media.url, media.expected_seconds), (
            "mp4", "https://cdn.example.test/video.mp4", None,
        ))

    def test_unknown_player_is_never_opened(self):
        browser = FixtureBrowser({LESSON: {"frames": [{"url": "https://outside.test/sign-player/3"}]}})
        with patch(FETCH_PLAYER) as fetch, self.assertRaisesRegex(UnsupportedMedia, "unrecognized"):
            self.provider.resolve(browser, self.ref, self.video)
        fetch.assert_not_called()
        self.assertEqual([url for url, _ in browser.calls], [LESSON])

    def test_native_video_uses_a_supported_source(self):
        mp4 = "https://cdn.example.test/play"
        browser = FixtureBrowser({
            LESSON: {"videos": [{"sources": [
                {"url": "https://cdn.example.test/play.mpd", "type": "application/dash+xml"},
                {"url": mp4, "type": "video/mp4; codecs=test"},
            ]}]},
        })
        lesson = self.provider.extract(browser, self.ref)
        media = self.provider.resolve(browser, self.ref, lesson.assets[0])
        self.assertEqual((media.kind, media.url, media.headers), ("mp4", mp4, {"Referer": LESSON}))

    @unittest.skipUnless(FIXTURES.is_dir(), "Jarvis's redacted workspace fixtures are unavailable")
    def test_redacted_bundle_module_and_lesson_projections(self):
        index = read_fixture("getcourse-index.snapshot.json")
        bundle = read_fixture("getcourse-bundle-935012532.json")
        module = read_fixture("getcourse-module-935012534.json")
        saved_lesson = read_fixture("getcourse-lesson-345516672.json")
        pages = {bundle["url"]: bundle["result"], module["url"]: module["result"]}
        children = [link for link in bundle["result"]["links"] if "/stream/view/id/" in link["url"]]
        for child in children:
            pages[child["url"]] = {"links": []}
        pages[children[0]["url"]]["links"].append({"text": "Module 1", "url": module["url"]})

        browser = FixtureBrowser(pages)
        with self.assertRaisesRegex(ValueError, "stream URL"):
            self.provider.discover(browser, index["url"])
        self.assertEqual(browser.calls, [])
        lessons = self.provider.discover(browser, bundle["url"])
        self.assertEqual([ref.lesson_id for ref in lessons], [
            "345516669", "345516670", "345516671", "345516672",
        ])
        ref = lessons[-1]
        self.assertEqual((ref.order, ref.title, ref.modules[-1]), (
            (3, 1, 2), saved_lesson["result"]["title"], module["result"]["title"],
        ))
        browser.pages[ref.url] = saved_lesson["result"]
        lesson = self.provider.extract(browser, ref)
        self.assertEqual((lesson.status, lesson.assets), ("available", (self.video,)))
        with self.assertRaisesRegex(UnsupportedMedia, "player URL is unavailable"):
            self.provider.resolve(browser, ref, lesson.assets[0])
