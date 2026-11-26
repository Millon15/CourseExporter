import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from course_exporter.publish import GIB, AdmissionTimeout, BudgetError, DrivePublisher, PublishError, SpaceBudget, SpaceGuard
from course_exporter.state import VerifiedFile


class FakeRclone:
    def __init__(self, remote_files=(), code=0):
        self.remote_files, self.code, self.calls, self.listings = list(remote_files), code, [], []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if "--files-from" in args:
            self.listings.append(Path(args[args.index("--files-from") + 1]).read_text().split())
        stdout = "\n".join("\t".join(map(str, row)) for row in self.remote_files) if args[1] == "lsf" else ""
        return subprocess.CompletedProcess(args, self.code, stdout, "Failed to copy: https://x.test/?token=SECRET")


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.lesson = self.out / "001.002-welcome"
        self.lesson.mkdir()
        for name in ("lesson.html", "v1-welcome.mp4", "i1-slide.png", "v2-extra.mp4.part"):
            (self.lesson / name).write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def verified(self, name, size=1, md5="aa"):
        return VerifiedFile(self.lesson / name, size, "0" * 64, 0.0, md5)

    def test_only_verified_files_move_then_the_page_is_copied_and_drive_is_listed(self):
        rclone = FakeRclone(remote_files=[("i1-slide.png", 1, "AA"), ("lesson.html", 9, "cc"), ("v1-welcome.mp4", 1, "aa")])
        DrivePublisher("gdrive:Courses/kwiga/", run=rclone).publish_lesson(
            self.out, self.lesson, [self.verified("v1-welcome.mp4"), self.verified("i1-slide.png")])
        move, copy, lsf = rclone.calls
        self.assertEqual(move[:4], ["rclone", "move", str(self.lesson), "gdrive:Courses/kwiga/001.002-welcome"])
        self.assertIn("--checksum", move)
        self.assertEqual(rclone.listings, [["i1-slide.png", "v1-welcome.mp4"]], "the .part file never moves")
        self.assertEqual(copy[:4], ["rclone", "copy", str(self.lesson / "lesson.html"),
                                    "gdrive:Courses/kwiga/001.002-welcome"])
        self.assertEqual(lsf[:3], ["rclone", "lsf", "gdrive:Courses/kwiga/001.002-welcome"])
        self.assertIn("MD5", lsf)

    def test_a_drive_file_with_other_bytes_fails_publication(self):
        for remote in ([("v1-welcome.mp4", 2, "aa")], [("v1-welcome.mp4", 1, "bb")], []):
            with self.assertRaises(PublishError, msg=remote):
                DrivePublisher("gdrive:C", run=FakeRclone(remote_files=remote)).publish_lesson(
                    self.out, self.lesson, [self.verified("v1-welcome.mp4")])

    def test_a_file_missing_on_drive_fails_and_a_retry_moves_only_what_is_still_staged(self):
        files = [self.verified("v1-welcome.mp4"), self.verified("i1-slide.png")]
        with self.assertRaises(PublishError):
            DrivePublisher("gdrive:C", run=FakeRclone(remote_files=[("v1-welcome.mp4", 1, "aa")])).publish_lesson(
                self.out, self.lesson, files)
        (self.lesson / "v1-welcome.mp4").rename(self.out / "gone.mp4")
        retry = FakeRclone(remote_files=[("v1-welcome.mp4", 1, "aa"), ("i1-slide.png", 1, "aa")])
        DrivePublisher("gdrive:C", run=retry).publish_lesson(self.out, self.lesson, files)
        self.assertEqual(retry.listings, [["i1-slide.png"]])

    def test_a_failed_rclone_raises_without_urls(self):
        with self.assertRaises(PublishError) as caught:
            DrivePublisher("gdrive:C", run=FakeRclone(code=1)).publish_index(self.out)
        self.assertNotIn("SECRET", str(caught.exception))


class SpaceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, free, used=0, pid=None, logs=None, sleeps=None):
        budget = SpaceBudget(self.root, min_free=20 * GIB, max_staging=5 * GIB)
        return SpaceGuard(budget, log=(logs if logs is not None else []).append,
                          sleep=(sleeps if sleeps is not None else []).append,
                          free_bytes=free if callable(free) else (lambda p: free),
                          used_bytes=lambda p: used, pid=pid)

    def test_two_jobs_count_each_others_reservations(self):
        first = self.guard(free=24 * GIB, pid=os.getpid())
        other_pid = os.getppid()
        free_after = iter([24 * GIB, 30 * GIB])
        logs, sleeps = [], []
        second = self.guard(free=lambda p: next(free_after), pid=other_pid, logs=logs, sleeps=sleeps)
        with first.reserve(3 * GIB):
            with second.reserve(2 * GIB):
                pass
        self.assertEqual(len(sleeps), 1, "24 GiB free minus 3 reserved minus 2 wanted is under the 20 GiB floor")
        self.assertTrue(logs[0].startswith("paused: 21.0 GiB free after other reservations"), logs)
        self.assertEqual(logs[-1], "resumed: staging has room")

    def test_a_lesson_that_never_gets_room_gives_up_after_the_wait(self):
        sleeps = []
        guard = self.guard(free=30 * GIB, used=5 * GIB, pid=os.getpid(), sleeps=sleeps)
        with self.assertRaises(AdmissionTimeout) as raised:
            with guard.reserve(GIB):
                pass
        self.assertEqual(sum(sleeps), 3600)
        self.assertIn("waited 60 min", str(raised.exception))
        self.assertEqual(list((self.root / ".reservations").glob("*.json")), [])

    def test_a_reservation_ends_with_its_lesson(self):
        guard = self.guard(free=30 * GIB, pid=os.getpid())
        with guard.reserve(GIB):
            self.assertEqual(len(list((self.root / ".reservations").glob("*.json"))), 1)
        self.assertEqual(list((self.root / ".reservations").glob("*.json")), [])

    def test_a_lesson_bigger_than_the_cap_is_refused_and_the_floor_reports_a_breach(self):
        with self.assertRaises(BudgetError):
            with self.guard(free=30 * GIB).reserve(6 * GIB):
                pass
        self.assertIsNone(self.guard(free=21 * GIB).breach())
        self.assertEqual(self.guard(free=19 * GIB).breach(), "19.0 GiB free, floor 20")

    def test_the_lesson_watcher_stops_at_the_floor_the_cap_or_the_reservation(self):
        lesson = self.root / "001-lesson"
        lesson.mkdir()
        (lesson / "v1.mp4.part").write_bytes(b"x" * 2048)
        self.assertIsNone(self.guard(free=30 * GIB, used=GIB).watcher(lesson, 4096)())
        self.assertIn("reserved", self.guard(free=30 * GIB, used=GIB).watcher(lesson, 1024)())
        self.assertIn("cap 5", self.guard(free=30 * GIB, used=6 * GIB).watcher(lesson, 4096)())
        self.assertIn("floor 20", self.guard(free=19 * GIB).watcher(lesson, 4096)())


if __name__ == "__main__":
    unittest.main()
