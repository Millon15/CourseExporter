import sqlite3
import tempfile
import unittest
from pathlib import Path

from course_exporter.model import Asset, LessonRef
from course_exporter.state import CourseState, VerifiedFile

REF = LessonRef("fake", "c", "L1", (1, 1), "Lesson", "https://example.test/1")
FILE = Asset("file-1", "file", "table.docx", "file-1")


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "state.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_published_lesson_stays_published_after_a_later_status(self):
        state = CourseState(self.path)
        state.record_discovery([REF])
        self.assertFalse(state.was_published("L1"))
        state.set_lesson("L1", "published")
        state.set_lesson("L1", "failed", "extract failed")
        self.assertTrue(state.was_published("L1"))
        state.close()

    def test_an_older_state_learns_which_lessons_were_published(self):
        old = sqlite3.connect(self.path)
        old.executescript("CREATE TABLE lessons (lesson_id TEXT PRIMARY KEY, position INTEGER NOT NULL, ref TEXT NOT"
                          " NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);"
                          "INSERT INTO lessons VALUES ('L1', 0, '{}', 'published', '', 'x'),"
                          " ('L2', 1, '{}', 'done', '', 'x');")
        old.close()
        state = CourseState(self.path)
        self.assertEqual((state.was_published("L1"), state.was_published("L2")), (True, False))
        state.close()

    def test_record_file_verifies_a_known_asset_in_place(self):
        state = CourseState(self.path)
        state.record_discovery([REF])
        state.mark_asset("L1", FILE, "unsupported", "HTTP 403")
        state.record_file("L1", "file-1", VerifiedFile(Path("/out/L1/file-1-table.docx"), 13108, "a" * 64, md5="b" * 32))
        self.assertEqual(state.asset_status("L1", "file-1"), "verified")
        self.assertEqual(state.verified_files("L1"),
                         [VerifiedFile(Path("/out/L1/file-1-table.docx"), 13108, "a" * 64, 0.0, "b" * 32)])
        with self.assertRaises(KeyError):
            state.record_file("L1", "file-9", VerifiedFile(Path("/x"), 1, ""))
        state.close()


if __name__ == "__main__":
    unittest.main()
