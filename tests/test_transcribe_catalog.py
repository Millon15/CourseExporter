import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from course_exporter.state import SCHEMA
from course_exporter.transcribe import catalog

NOW = "2026-11-27T00:00:00+00:00"


def lesson(db, lesson_id, position, status, title):
    ref = {"provider": "kwiga", "course_id": "c", "lesson_id": lesson_id, "order": [position], "title": title,
           "url": "https://x.test", "modules": []}
    db.execute("INSERT INTO lessons VALUES (?, ?, ?, ?, '', ?)", (lesson_id, position, json.dumps(ref), status, NOW))


def asset(db, course, lesson_id, asset_id, kind, status, lesson_dir, name):
    db.execute("INSERT INTO assets VALUES (?, ?, ?, 'T', ?, ?, 10, '', 'ABCDEF', 61.5, '', ?)",
               (lesson_id, asset_id, kind, status, str(course / lesson_dir / name), NOW))


class PublishedVideosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / "kwiga-smm"
        self.course.mkdir()
        with sqlite3.connect(self.course / catalog.STATE_FILE) as db:
            db.executescript(SCHEMA)
            lesson(db, "b", 2, "published", "Урок 2")
            lesson(db, "a", 1, "published", "Урок 1")
            lesson(db, "c", 3, "done", "Урок 3")
            asset(db, self.course, "a", "v1", "video", "verified", "001-урок-1", "v1.mp4")
            asset(db, self.course, "a", "i1", "image", "verified", "001-урок-1", "i1.png")
            asset(db, self.course, "b", "v2", "video", "verified", "002-урок-2", "v2.mp4")
            asset(db, self.course, "b", "v3", "video", "failed", "002-урок-2", "v3.mp4")
            asset(db, self.course, "c", "v4", "video", "verified", "003-урок-3", "v4.mp4")

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_verified_videos_of_published_lessons_in_course_order(self):
        videos = catalog.published_videos(self.course)
        self.assertEqual([(v.lesson_id, v.asset_id) for v in videos], [("a", "v1"), ("b", "v2")])
        first = videos[0]
        self.assertEqual((first.provider, first.course, first.lesson_title, first.lesson_dir, first.name),
                         ("kwiga", "kwiga-smm", "Урок 1", "001-урок-1", "v1.mp4"))
        self.assertEqual((first.bytes, first.md5, first.duration), (10, "abcdef", 61.5))
        self.assertEqual(first.drive_folder("gdrive:Courses/"), "gdrive:Courses/kwiga-smm/001-урок-1")

    def test_leaves_the_state_file_byte_identical(self):
        before = hashlib.sha256((self.course / catalog.STATE_FILE).read_bytes()).hexdigest()
        catalog.published_videos(self.course)
        self.assertEqual(hashlib.sha256((self.course / catalog.STATE_FILE).read_bytes()).hexdigest(), before)

    def test_exact_revision_must_remain_published_during_refresh(self):
        video = catalog.published_videos(self.course)[0]
        self.assertTrue(catalog.is_published(self.course, video))
        with sqlite3.connect(self.course / catalog.STATE_FILE) as db:
            db.execute("UPDATE lessons SET status='partial' WHERE lesson_id='a'")
        self.assertFalse(catalog.is_published(self.course, video))
        with sqlite3.connect(self.course / catalog.STATE_FILE) as db:
            db.execute("UPDATE lessons SET status='published' WHERE lesson_id='a'")
            db.execute("UPDATE assets SET md5='new' WHERE asset_id='v1'")
        self.assertFalse(catalog.is_published(self.course, video))

    def test_missing_state_is_an_error_not_a_new_database(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError):
            catalog.published_videos(empty)
        self.assertFalse((empty / catalog.STATE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
