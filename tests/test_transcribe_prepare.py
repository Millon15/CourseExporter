import fcntl
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from unittest import mock

from course_exporter.transcribe import prepare
from course_exporter.transcribe.source import AudioCache, LowSpace
from tests.test_transcribe_source import asset, drive_file


class MaintenanceTest(unittest.TestCase):
    def test_vancouver_window_respects_dst_and_exact_edges(self):
        for instant, deferred in (("2025-11-28T08:59:00+00:00", False), ("2025-11-28T09:00:00+00:00", True),
                                  ("2025-11-28T12:54:59+00:00", True), ("2025-11-28T12:55:00+00:00", False),
                                  ("2026-07-01T08:00:00+00:00", True)):
            self.assertEqual(prepare.window(datetime.fromisoformat(instant)), deferred)

    def test_monday_memory_index_has_thirty_minutes_of_clearance(self):
        monday = datetime(2026, 11, 30, 1, 30, tzinfo=prepare.VANCOUVER)
        self.assertFalse(prepare.window(monday.replace(hour=0, minute=59)))
        self.assertTrue(prepare.window(monday.replace(minute=0)))
        self.assertTrue(prepare.window(monday))

    def test_live_cron_and_fleet_lock_block_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            proc = home / "proc/42"
            proc.mkdir(parents=True)
            (proc / "cmdline").write_bytes(b"/usr/bin/bash\x00/path/cron-run\x00restore-drill\x00")
            self.assertTrue(prepare.maintenance_running(proc.parent))
            fleet = home / ".openclaw/workspace/run/fleet-maintenance.lock"
            fleet.parent.mkdir(parents=True)
            (home / ".openclaw/updates-state").mkdir()
            now = datetime.fromisoformat("2026-11-28T21:00:00+00:00")
            with fleet.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                with prepare.admission(home, home, home / "cache", now, active=lambda: False) as reason:
                    self.assertIn("lock is held", reason)
            with prepare.admission(home, home, home / "cache", now, active=lambda: True) as reason:
                self.assertIn("still running", reason)

    def test_space_floor_counts_live_exporter_reservations_without_writing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            free = prepare.MIN_FREE_BYTES + 128
            with mock.patch.object(prepare.shutil, "disk_usage", return_value=mock.Mock(free=free)), \
                    mock.patch.object(prepare.SpaceGuard, "_others", return_value=128):
                prepare.check_headroom(root, root / "cache", 0)
                with self.assertRaises(LowSpace):
                    prepare.check_headroom(root, root / "cache", 1)
            self.assertFalse((root / ".reservations").exists())


class DurationTest(unittest.TestCase):
    def test_preparation_uses_exporter_duration_first_then_pinned_drive_duration(self):
        cases = ((12.3, 13.0, 12.3), (0.0, 1198.33, 1198.33), (0.0, 740.599, 740.599))
        for exporter_seconds, drive_seconds, expected in cases:
            with self.subTest(exporter_seconds=exporter_seconds, drive_seconds=drive_seconds), \
                    tempfile.TemporaryDirectory() as directory:
                job, journal, publisher = self.preparation(Path(directory), exporter_seconds, drive_seconds)
                cache = mock.Mock()
                audio = mock.sentinel.cached_audio
                cache.audio.side_effect = [LowSpace("cache is full"), audio]
                prepare.run(journal, cache, publisher, nullcontext)
                self.assertEqual(cache.audio.call_args_list, [mock.call(job.source, expected)] * 2)
                journal.ready.assert_called_once_with(job.id, audio)

    def test_preparation_fails_if_neither_duration_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job, journal, publisher = self.preparation(root, 0.0, None)
            cache = AudioCache(root / "cache", mock.Mock())
            prepare.run(journal, cache, publisher, nullcontext)
            journal.mark.assert_called_once_with(job.id, "preparing", "failed",
                                                 "preparation: missing verified video duration")
            journal.ready.assert_not_called()

    @staticmethod
    def preparation(root, exporter_seconds, drive_seconds):
        job = mock.Mock(id="job", asset=asset(duration=exporter_seconds),
                        source=drive_file(video_seconds=drive_seconds))
        journal = mock.Mock(path=root / "journal.sqlite3")
        journal.next_publish.return_value = None
        journal.next_prepare.side_effect = [job, None]
        journal.report.return_value = []
        publisher = mock.Mock()
        publisher.already_compliant.return_value = False
        return job, journal, publisher
