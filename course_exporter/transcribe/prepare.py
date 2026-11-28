"""Run preparation and pending publication under the VPS service limits."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from ..publish import SpaceBudget, SpaceGuard
from . import adapter
from .journal import TERMINAL, Journal
from .publisher import PublishError, Publisher, Retryable
from .source import MIN_FREE_BYTES, AudioCache, LowSpace, SourceChanged, SourceError, TranscodeError

UNIT = "course-transcribe-prepare.service"
VANCOUVER = ZoneInfo("America/Vancouver")
MAINTENANCE_JOBS = frozenset({b"backup", b"restore-drill", b"weekly-update"})
MAINTENANCE_PROGRAMS = frozenset({b"backup.sh", b"restore-drill", b"jarvis-weekly-update-with-retry",
                                 b"fleet-os-gate"})
MAX_PUBLISH_ATTEMPTS = 3


def wake() -> None:
    subprocess.run(["systemctl", "--user", "start", "--no-block", UNIT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)


def maintenance_running(proc: Path = Path("/proc")) -> bool:
    """Recognize the live cron command or its maintenance program, including overruns."""
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            argv = (path / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        # A shebang interpreter can precede the actual command.
        for index, word in enumerate(argv[:2]):
            name = word.rsplit(b"/", 1)[-1]
            if name in MAINTENANCE_PROGRAMS:
                return True
            if name == b"cron-run" and index + 1 < len(argv) and argv[index + 1] in MAINTENANCE_JOBS:
                return True
    return False


def window(now: datetime) -> bool:
    local = now.astimezone(VANCOUVER)
    # From 01:00, a new run could overlap Monday's 01:30 fleet-memory-index lock acquisition.
    return 60 <= local.hour * 60 + local.minute < 295


@contextmanager
def admission(home: Path, staging: Path, cache: Path, now: datetime | None = None,
              active: Callable[[], bool] = maintenance_running) -> Iterator[str | None]:
    """Hold maintenance locks only while the VPS works. Inspect the staging floor without editing staging."""
    if window(now or datetime.now(VANCOUVER)):
        yield "Vancouver maintenance window (01:00–04:55)"
        return
    lock_paths = (home / ".openclaw/workspace/run/fleet-maintenance.lock",
                  home / ".openclaw/updates-state/weekly-update.lock")
    with ExitStack() as stack:
        try:
            for path in lock_paths:
                fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield "fleet maintenance or weekly updater lock is held"
            return
        except OSError:
            yield "cannot check the fleet maintenance locks"
            return
        if active():
            yield "backup, restore drill or updater is still running"
            return
        # The cache may live on a different filesystem from exporter staging.
        try:
            check_headroom(staging, cache, 0)
        except LowSpace as exc:
            yield str(exc)
            return
        yield None


def check_headroom(staging: Path, cache: Path, allowance: int) -> None:
    """Leave the floor free after current exporter reservations and the next Opus write."""
    guard = SpaceGuard(SpaceBudget(staging))
    # The exporter owns the reservation directory. This reads existing live reservations only.
    staging_free = shutil.disk_usage(staging).free - guard._others()
    cache_free = shutil.disk_usage(cache.parent).free
    if staging.stat().st_dev == cache.parent.stat().st_dev:
        staging_free -= allowance
    if min(staging_free, cache_free - allowance) < MIN_FREE_BYTES:
        raise LowSpace("15 GiB disk floor after accounting for exporter reservations")


def run(journal: Journal, cache: AudioCache, publisher: Publisher, admit: Callable) -> None:
    """Drain the pending result queue, then fill at most one ready audio slot."""
    with journal.path.with_name("prepare.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with admit() as reason:
            journal.pause(reason)
            if reason is not None:
                return
            while pending := journal.next_publish():
                job, data = pending
                try:
                    publisher.publish(job, data)
                except SourceChanged as exc:
                    journal.mark(job.id, "publishing", "replaced/unavailable", str(exc))
                except Retryable as exc:
                    if job.publish_attempts < MAX_PUBLISH_ATTEMPTS:
                        return
                    journal.mark(job.id, "publishing", "failed", f"publication: {exc}")
                except adapter.Refused as exc:
                    journal.mark(job.id, "publishing", "failed", f"validation: {exc}")
                except (PublishError, SourceError) as exc:
                    journal.mark(job.id, "publishing", "failed", f"publication: {exc}")
                else:
                    journal.mark(job.id, "publishing", "verified", None)
            while job := journal.next_prepare():
                try:
                    if publisher.already_compliant(job):
                        journal.mark(job.id, "preparing", "already compliant", "verified selection on Drive")
                        continue
                    expected_seconds = job.asset.duration
                    if expected_seconds <= 0:
                        expected_seconds = job.source.video_seconds or 0.0
                    try:
                        audio = cache.audio(job.source, expected_seconds)
                    except LowSpace:
                        live = journal.report(scoped=False)
                        active_sources = {row.source for row in live if row.state not in TERMINAL}
                        for row in live:
                            if row.state in TERMINAL and row.source not in active_sources:
                                cache.discard(row.source)
                        audio = cache.audio(job.source, expected_seconds)
                    publisher.current(job)
                    journal.ready(job.id, audio)
                    return
                except LowSpace as exc:
                    journal.mark(job.id, "preparing", "deferred", str(exc))
                    return
                except SourceChanged as exc:
                    journal.mark(job.id, "preparing", "replaced/unavailable", str(exc))
                except (SourceError, TranscodeError, PublishError) as exc:
                    journal.mark(job.id, "preparing", "failed", f"preparation: {exc}")
