import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from course_exporter import archive, cli
from course_exporter.model import Asset, LessonRef
from course_exporter.state import CourseState

REF = LessonRef("getcourse", "c", "345", (5, 3), "Урок 2", "https://school.test/345")


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        state = CourseState(self.out / cli.STATE_FILE)
        state.record_discovery([REF])
        state.mark_asset("345", Asset("file-1", "file", "СКАЧАТЬ ТАБЛИЦУ", "file-1"), "unsupported", "HTTP 403")
        state.close()
        self.env = mock.patch.dict(os.environ, {"COURSE_EXPORTER_CONFIG": str(self.out / "none.toml")})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def record(self, *extra: str) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return cli.main(["record", "--out", str(self.out), "--lesson", "345", "--asset", "file-1",
                             "--bytes", "13108", "--md5", "ABC", *extra])

    def test_records_the_drive_copy_under_the_lesson_folder(self):
        self.assertEqual(self.record("--name", "file-1-Рабочая таблица.docx"), 0)
        state = CourseState(self.out / cli.STATE_FILE)
        [file] = state.verified_files("345")
        state.close()
        self.assertEqual(file.path, archive.lesson_dir(self.out, REF) / "file-1-Рабочая таблица.docx")
        self.assertEqual((file.bytes, file.md5), (13108, "abc"))

    def test_refuses_a_path_and_an_unknown_asset(self):
        self.assertEqual(self.record("--name", "../escape.docx"), cli.EXIT_USAGE)
        self.assertEqual(self.record("--name", "x.docx", "--asset", "file-9"), cli.EXIT_USAGE)

if __name__ == "__main__":
    unittest.main()
