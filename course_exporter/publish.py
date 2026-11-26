"""Moves finished lessons from VPS staging to the owner's Google Drive and keeps staging inside its disk budget."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Sequence

from .media import redact
from .state import VerifiedFile

Runner = Callable[..., subprocess.CompletedProcess]
GIB = 1 << 30
LESSON_PAGE = "lesson.html"
RESERVATIONS = ".reservations"


class PublishError(Exception):
    """rclone failed; the message is redacted. Files Drive already holds may be gone from staging; the rest stay."""


class AdmissionTimeout(Exception):
    """No room opened for a lesson within the wait; the run stops and a rerun resumes at that lesson."""


class BudgetError(Exception):
    """A lesson needs more staging than the whole budget allows."""


class DrivePublisher:
    """Moves the verified files named by state with `rclone move --checksum` (a staged file is deleted only after
    Drive holds the same hash), copies lesson.html (it stays local for the index), then fails unless Drive reports
    every verified file with the size and MD5 that state recorded."""

    def __init__(self, remote: str, rclone: str = "rclone", run: Runner = subprocess.run) -> None:
        self._remote = remote.rstrip("/")
        self._rclone = rclone
        self._run = run

    def publish_lesson(self, out: Path, lesson_dir: Path, files: Sequence[VerifiedFile]) -> None:
        target = f"{self._remote}/{lesson_dir.relative_to(out).as_posix()}"
        expected = {f.path.relative_to(lesson_dir).as_posix(): (f.bytes, f.md5) for f in files}
        staged = [name for name in sorted(expected) if (lesson_dir / name).is_file()]
        if staged:
            with tempfile.NamedTemporaryFile("w", dir=out, prefix=".files-", delete=True) as listing:
                listing.write("\n".join(staged) + "\n")
                listing.flush()
                self._call("move", str(lesson_dir), target, "--files-from", listing.name)
        self._call("copy", str(lesson_dir / LESSON_PAGE), target)
        remote = self._remote_files(target)
        wrong = [name for name, (size, md5) in expected.items()
                 if remote.get(name, (None, None)) != (size, md5 or remote.get(name, (None, None))[1])]
        if wrong:
            raise PublishError(f"{len(wrong)} verified file(s) missing on Drive or differing in size or MD5")

    def publish_index(self, out: Path) -> None:
        self._call("copy", str(out / "index.html"), self._remote)

    def _remote_files(self, target: str) -> dict[str, tuple[int, str]]:
        """name -> (size, md5) as Drive reports them."""
        result = self._run([self._rclone, "lsf", target, "--files-only", "-R", "--format", "psh", "--hash", "MD5",
                            "--separator", "\t"], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise PublishError(f"rclone lsf exit {result.returncode}: {redact(result.stderr, 400)}")
        files = {}
        for line in result.stdout.splitlines():
            name, size, md5 = (line.split("\t") + ["", ""])[:3]
            if size.isdigit():
                files[name] = (int(size), md5.lower())
        return files

    def _call(self, verb: str, source: str, target: str, *extra: str) -> None:
        args = [self._rclone, verb, source, target, "--checksum", "--retries", "5", "--low-level-retries", "500", *extra]
        result = self._run(args, capture_output=True, text=True, timeout=6 * 3600)
        if result.returncode != 0:
            raise PublishError(f"rclone {verb} exit {result.returncode}: {redact(result.stderr, 400)}")


@dataclass(frozen=True)
class SpaceBudget:
    """Staging may hold at most `max_staging` bytes and must leave `min_free` bytes free on its disk, counting every
    exporter's reservation; the default floor protects the nightly backups that share the disk."""

    root: Path
    min_free: int = 20 * GIB
    max_staging: int = 5 * GIB
    poll_seconds: int = 60
    max_wait_seconds: int = 3600
    """A lesson waiting this long for room ends the run, so a supervisor can rerun it; a partial lesson elsewhere may
    hold the room, and only its own retry publishes it."""


class SpaceGuard:
    """Admits one lesson at a time per process against reservations that all exporters share through a locked
    directory, so two jobs cannot both pass the same headroom; `breach` is the floor checked during a transfer."""

    def __init__(self, budget: SpaceBudget, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep,
                 free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
                 used_bytes: Callable[[Path], int] | None = None, pid: int | None = None) -> None:
        self._budget = budget
        self._log = log
        self._sleep = sleep
        self._free_bytes = free_bytes
        self._used_bytes = used_bytes or _tree_bytes
        self._pid = pid or os.getpid()
        self._dir = budget.root / RESERVATIONS

    @contextmanager
    def reserve(self, size: int) -> Generator[None]:
        if size > self._budget.max_staging:
            raise BudgetError(f"lesson needs {size / GIB:.1f} GiB, staging cap is {self._budget.max_staging / GIB:.0f}")
        self._dir.mkdir(parents=True, exist_ok=True)
        announced, waited = False, 0
        while (reason := self._admit(size)) is not None:
            if waited >= self._budget.max_wait_seconds:
                raise AdmissionTimeout(f"waited {waited // 60} min for staging room: {reason}")
            if not announced:
                self._log(f"paused: {reason}")
                announced = True
            self._sleep(self._budget.poll_seconds)
            waited += self._budget.poll_seconds
        if announced:
            self._log("resumed: staging has room")
        try:
            yield
        finally:
            (self._dir / f"{self._pid}.json").unlink(missing_ok=True)

    def breach(self) -> str | None:
        free = self._free_bytes(self._budget.root)
        return None if free >= self._budget.min_free else f"{free / GIB:.1f} GiB free, floor {self._budget.min_free / GIB:.0f}"

    def watcher(self, lesson_dir: Path, allowance: int) -> Callable[[], str | None]:
        """The abort check for one lesson's writers: the disk floor, the staging cap, and the lesson's own reservation
        (temporary and merge files included, since they live in the lesson directory)."""
        def check() -> str | None:
            if reason := self.breach():
                return reason
            if (used := self._used_bytes(self._budget.root)) > self._budget.max_staging:
                return f"staging holds {used / GIB:.1f} GiB, cap {self._budget.max_staging / GIB:.0f}"
            if (written := _tree_bytes(lesson_dir)) > allowance:
                return f"lesson wrote {written / GIB:.2f} GiB, reserved {allowance / GIB:.2f}"
            return None
        return check

    def _admit(self, size: int) -> str | None:
        with (self._dir / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            others = self._others()
            free = self._free_bytes(self._budget.root) - others - size
            used = self._used_bytes(self._budget.root) + others + size
            if free < self._budget.min_free:
                return f"{(free + size) / GIB:.1f} GiB free after other reservations, need {size / GIB:.1f} above the {self._budget.min_free / GIB:.0f} GiB floor"
            if used > self._budget.max_staging:
                return f"staging would hold {used / GIB:.1f} GiB, cap {self._budget.max_staging / GIB:.0f}"
            (self._dir / f"{self._pid}.json").write_text(json.dumps({"pid": self._pid, "bytes": size}))
            return None

    def _others(self) -> int:
        total = 0
        for entry in self._dir.glob("*.json"):
            try:
                record = json.loads(entry.read_text())
            except (OSError, ValueError):
                continue
            if record.get("pid") != self._pid and _alive(record.get("pid")):
                total += int(record.get("bytes", 0))
        return total


def _alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total
