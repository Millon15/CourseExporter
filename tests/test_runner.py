import tempfile
import unittest
from pathlib import Path

from course_exporter import media
from course_exporter.model import Asset, Lesson, LessonRef, Media, UnsupportedMedia
from contextlib import contextmanager

from course_exporter.publish import BudgetError, PublishError
from course_exporter.runner import Exporter, ExportOptions, estimate_bytes
from course_exporter.state import CourseState, VerifiedFile


def ref(n: int) -> LessonRef:
    return LessonRef(provider="fake", course_id="c", lesson_id=f"L{n}", order=(1, n), title=f"Lesson {n}",
                     url=f"https://example.test/lesson/{n}")


def video(asset_id: str) -> Asset:
    return Asset(asset_id=asset_id, kind="video", title=f"{asset_id}.mp4", ref=asset_id)


class FakeProvider:
    name = "fake"

    def __init__(self, lessons: dict[str, Lesson], unsupported=(), broken=()) -> None:
        self.lessons, self.unsupported, self.broken = lessons, set(unsupported), set(broken)
        self.resolved: list[str] = []

    def discover(self, browser, course_url):
        return [lesson.ref for lesson in self.lessons.values()]

    def extract(self, browser, lesson_ref):
        if lesson_ref.lesson_id in self.broken:
            raise ValueError("no lesson_dto at https://signed.example.test/x?token=SECRET")
        return self.lessons[lesson_ref.lesson_id]

    def resolve(self, browser, lesson_ref, asset):
        if asset.asset_id in self.unsupported:
            raise UnsupportedMedia("DRM license required")
        self.resolved.append(asset.asset_id)
        return Media(kind="hls", url=f"https://signed.example.test/{asset.asset_id}?sig=SECRET")


class FakeArchive:
    def __init__(self) -> None:
        self.lessons: dict[str, dict[str, Path]] = {}
        self.index: list[tuple[str, str]] = []

    def lesson_dir(self, out, lesson_ref):
        return out / lesson_ref.lesson_id

    def write_lesson(self, out, lesson, files):
        self.lessons[lesson.ref.lesson_id] = dict(files)
        return out / lesson.ref.lesson_id / "lesson.html"

    def write_index(self, out, entries):
        self.index = [(r.lesson_id, status) for r, status in entries]
        return out / "index.html"


class ExporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.state = CourseState(self.out / "state.sqlite3")
        self.fetched: list[str] = []
        self.fetch_aborts: list = []
        self.failing_fetch: set[str] = set()
        self.bad_files: set[str] = set()
        self.logs: list[str] = []

    def tearDown(self) -> None:
        self.state.close()
        self.tmp.cleanup()

    def fetch(self, source, asset, stem, max_height, abort=None):
        self.fetched.append(asset.asset_id)
        self.fetch_aborts.append(abort)
        if asset.asset_id in self.failing_fetch:
            raise media.DownloadError("yt-dlp exit 1: HTTP 403 for <url>")
        stem.parent.mkdir(parents=True, exist_ok=True)
        path = stem.with_name(stem.name + ".mp4")
        path.write_bytes(b"video")
        return path

    def check(self, path, kind, decode=False, expected_seconds=None):
        if any(path.name.startswith(bad) for bad in self.bad_files):
            raise media.VerificationError(f"{path.name}: no video stream")
        return VerifiedFile(path=path, bytes=path.stat().st_size, sha256="0" * 64, duration=12.0)

    def exporter(self, provider, publisher=None, space=None):
        return Exporter(provider, browser=object(), state=self.state, archive=self.archive, fetch=self.fetch,
                        check=self.check, log=self.logs.append, publisher=publisher, space=space)

    def course(self, *lessons: Lesson, **kwargs):
        self.archive = FakeArchive()
        return FakeProvider({lesson.ref.lesson_id: lesson for lesson in lessons}, **kwargs)

    def options(self, **overrides):
        return ExportOptions(course_url="https://example.test/course", out=self.out, **overrides)

    def test_exports_every_lesson_in_order_and_writes_the_index(self):
        provider = self.course(Lesson(ref(1), "available", "<p>one</p>", (video("v1"),)),
                               Lesson(ref(2), "available", "<p>two</p>", ()))
        summary = self.exporter(provider).run(self.options())
        self.assertTrue(summary.complete)
        self.assertEqual(self.archive.index, [("L1", "done"), ("L2", "done")])
        self.assertEqual(list(self.archive.lessons["L1"]), ["v1"])

    def test_a_rerun_skips_done_lessons_and_verified_assets(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"),)),
                               Lesson(ref(2), "available", "", (video("v2"),)))
        self.failing_fetch = {"v2"}
        first = self.exporter(provider).run(self.options())
        self.assertEqual(first.lessons, {"done": 1, "partial": 1})
        self.failing_fetch = set()
        second = self.exporter(provider).run(self.options())
        self.assertTrue(second.complete)
        self.assertEqual(self.fetched, ["v1", "v2", "v2"])

    def test_unsupported_media_finishes_the_lesson_without_a_file(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("drm"), video("ok"))), unsupported={"drm"})
        summary = self.exporter(provider).run(self.options())
        self.assertEqual(summary.assets, {"unsupported": 1, "verified": 1})
        self.assertTrue(summary.complete)
        self.assertEqual(summary.gaps, 1)
        self.assertEqual(list(self.archive.lessons["L1"]), ["ok"])

    def test_a_file_that_fails_verification_is_set_aside_for_the_next_run(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"),)))
        self.bad_files = {"v1"}
        summary = self.exporter(provider).run(self.options())
        self.assertEqual(summary.assets, {"failed": 1})
        lesson_dir = self.out / "L1"
        self.assertEqual(sorted(p.name for p in lesson_dir.iterdir()), ["v1-v1.mp4.rejected"])

    def test_stop_after_videos_ends_the_pilot_at_the_first_verified_video(self):
        provider = self.course(Lesson(ref(1), "available", "text only", ()),
                               Lesson(ref(2), "available", "", (video("v2"), video("v2b"))),
                               Lesson(ref(3), "available", "", (video("v3"),)))
        summary = self.exporter(provider).run(self.options(stop_after_videos=1))
        self.assertEqual(self.fetched, ["v2"], "the second video of the same lesson waits")
        self.assertEqual(summary.lessons, {"done": 1, "partial": 1, "discovered": 1})
        self.assertFalse(summary.complete)

    def test_gated_and_broken_lessons_are_recorded_and_the_run_goes_on(self):
        provider = self.course(Lesson(ref(1), "gated", "", ()), Lesson(ref(2), "available", "", ()),
                               Lesson(ref(3), "available", "", (video("v3"),)), broken={"L2"})
        summary = self.exporter(provider).run(self.options())
        self.assertEqual(summary.lessons, {"gated": 1, "failed": 1, "done": 1})
        failure = next(line for line in self.logs if "extract failed" in line)
        self.assertIn("<url>", failure)
        self.assertNotIn("SECRET", " ".join(self.logs))


    def test_rediscovery_reopens_a_gated_lesson_the_owner_unlocked(self):
        provider = self.course(Lesson(ref(1), "gated", "", ()), Lesson(ref(2), "available", "", (video("v2"),)))
        self.exporter(provider).run(self.options())
        provider.lessons["L1"] = Lesson(ref(1), "available", "", (video("v1"),))
        summary = self.exporter(provider).run(self.options(rediscover=True))
        self.assertEqual(summary.lessons, {"done": 2})
        self.assertEqual(self.fetched, ["v2", "v1"])

    def test_finished_lessons_go_to_drive_and_a_failed_upload_is_retried_next_run(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"),)),
                               Lesson(ref(2), "available", "", (video("v2"),)))
        publisher = FakePublisher(fail_once={"L2"})
        first = self.exporter(provider, publisher=publisher).run(self.options())
        self.assertEqual(first.lessons, {"published": 1, "done": 1})
        self.assertFalse(first.complete)
        second = self.exporter(provider, publisher=publisher).run(self.options())
        self.assertEqual(second.lessons, {"published": 2})
        self.assertTrue(second.complete)
        self.assertEqual(self.fetched, ["v1", "v2"], "a re-upload never downloads again")
        self.assertEqual(publisher.lessons, ["L1", "L2", "L2"])
        self.assertEqual([f.path for f in publisher.files["L2"]], [self.out / "L2" / "v2-v2.mp4"],
                         "only verified files from state")
        self.assertEqual(publisher.indexes, 2)

    def test_a_failed_index_upload_keeps_the_run_incomplete(self):
        provider = self.course(Lesson(ref(1), "available", "", ()))
        summary = self.exporter(provider, publisher=FakePublisher(index_fails=True)).run(self.options())
        self.assertEqual(summary.lessons, {"published": 1})
        self.assertFalse(summary.complete)

    def test_a_lesson_reserves_staging_for_its_pending_videos(self):
        long_video = Asset("v1", "video", "v1.mp4", "v1", expected_seconds=8228)
        provider = self.course(Lesson(ref(1), "available", "", (long_video, Asset("i1", "image", "i.png", "i1"))))
        space = FakeSpace()
        self.exporter(provider, space=space).run(self.options())
        self.assertEqual(space.sizes, [estimate_bytes([long_video, Asset("i1", "image", "i.png", "i1")], 720)])
        self.assertGreater(space.sizes[0], 4 * 2**30)
        self.assertEqual(len(self.fetch_aborts), 2)
        space.floor = "3.0 GiB free, floor 20"
        self.assertEqual(self.fetch_aborts[0](), "3.0 GiB free, floor 20", "downloads poll the staging floor")

    def test_a_budget_stop_ends_the_run_and_discards_the_partial_files(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"), video("v2"))),
                               Lesson(ref(2), "available", "", (video("v3"),)))
        space = FakeSpace()
        exporter = self.exporter(provider, space=space)

        def fetch(source, asset, stem, max_height, abort=None):
            self.fetched.append(asset.asset_id)
            stem.parent.mkdir(parents=True, exist_ok=True)
            stem.with_name(stem.name + ".f137.mp4.part").write_bytes(b"half")
            raise media.Aborted("3.0 GiB free, floor 20")

        exporter._fetch = fetch
        summary = exporter.run(self.options())
        self.assertEqual(self.fetched, ["v1"], "no further writer starts after the stop")
        self.assertEqual(summary.stopped, "3.0 GiB free, floor 20")
        self.assertFalse(summary.complete)
        self.assertEqual(summary.lessons, {"partial": 1, "discovered": 1})
        self.assertEqual(list((self.out / "L1").glob("*.part")), [])

    def test_a_breach_before_a_writer_starts_stops_without_resolving(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"),)))
        space = FakeSpace()
        space.floor = "19.0 GiB free, floor 20"
        summary = self.exporter(provider, space=space).run(self.options())
        self.assertEqual((provider.resolved, self.fetched), ([], []))
        self.assertEqual(summary.stopped, "19.0 GiB free, floor 20")

    def test_a_lesson_over_the_whole_budget_fails_without_downloading(self):
        provider = self.course(Lesson(ref(1), "available", "", (video("v1"),)))
        summary = self.exporter(provider, space=FakeSpace(too_big=True)).run(self.options())
        self.assertEqual(summary.lessons, {"failed": 1})
        self.assertEqual(self.fetched, [])

    def test_the_space_guard_admits_every_available_lesson_once(self):
        provider = self.course(Lesson(ref(1), "available", "", ()), Lesson(ref(2), "gated", "", ()))
        space = FakeSpace()
        self.exporter(provider, space=space).run(self.options())
        self.exporter(provider, space=space).run(self.options())
        self.assertEqual(len(space.sizes), 1, "the gated lesson and the rerun reserve nothing")


class FakePublisher:
    def __init__(self, fail_once=(), index_fails=False) -> None:
        self.fail_once, self.index_fails, self.lessons, self.files, self.indexes = set(fail_once), index_fails, [], {}, 0

    def publish_lesson(self, out, lesson_dir, files):
        self.lessons.append(lesson_dir.name)
        self.files[lesson_dir.name] = list(files)
        if lesson_dir.name in self.fail_once:
            self.fail_once.discard(lesson_dir.name)
            raise PublishError("rclone move exit 1: quota")

    def publish_index(self, out):
        if self.index_fails:
            raise PublishError("rclone copy exit 1")
        self.indexes += 1


class FakeSpace:
    def __init__(self, too_big=False) -> None:
        self.too_big, self.sizes, self.floor, self.allowances = too_big, [], None, []

    @contextmanager
    def reserve(self, size):
        if self.too_big:
            raise BudgetError("lesson needs 9.0 GiB, staging cap is 5")
        self.sizes.append(size)
        yield

    def breach(self):
        return self.floor

    def watcher(self, lesson_dir, allowance):
        self.allowances.append(allowance)
        return self.breach


if __name__ == "__main__":
    unittest.main()
