"""Exports one course lesson by lesson, in course order, resuming from the per-course state."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Protocol, Sequence

from . import media
from .model import Asset, Browser, Lesson, LessonRef, Provider, UnsupportedMedia
from .publish import BudgetError, DrivePublisher, PublishError, SpaceGuard
from .state import CourseState, LessonStatus, VerifiedFile

MIB = 1 << 20
UNKNOWN_VIDEO_BYTES = 1024 * MIB
FILE_BYTES = 64 * MIB
LESSON_OVERHEAD_BYTES = 64 * MIB
MERGE_FACTOR = 2
"""yt-dlp holds the fragments and the merged or fixed-up MP4 at the same time."""


class ArchiveWriter(Protocol):
    def lesson_dir(self, out: Path, ref: LessonRef) -> Path: ...

    def write_lesson(self, out: Path, lesson: Lesson, files: dict[str, Path]) -> Path: ...

    def write_index(self, out: Path, entries: Sequence[tuple[LessonRef, str]]) -> Path: ...


@dataclass(frozen=True)
class ExportOptions:
    course_url: str
    out: Path
    max_height: int = 720
    limit: int | None = None
    """Stop after this many lessons, counted in course order."""
    stop_after_videos: int | None = None
    """Stop once this many videos were verified in this run (the pilot uses 1); later assets stay pending."""
    rediscover: bool = False
    decode: bool = False


@dataclass(frozen=True)
class ExportSummary:
    lessons: dict[str, int]
    assets: dict[str, int]
    complete: bool
    """Every lesson finished (published, or done without a publisher, or gated), no asset failed, index uploaded."""
    gaps: int
    """Assets recorded as unsupported (DRM, encryption, unknown player): finished, but not in the archive."""
    stopped: str | None = None
    """Why the staging budget ended the run early; a rerun resumes at that lesson."""


class Exporter:
    def __init__(self, provider: Provider, browser: Browser, state: CourseState, archive: ArchiveWriter,
                 fetch: Callable[..., Path] = media.download, check: Callable[..., VerifiedFile] = media.verify,
                 log: Callable[[str], None] = print, publisher: DrivePublisher | None = None,
                 space: SpaceGuard | None = None) -> None:
        self._provider = provider
        self._browser = browser
        self._state = state
        self._archive = archive
        self._fetch = fetch
        self._check = check
        self._log = log
        self._publisher = publisher
        self._space = space
        self._videos_verified = 0
        self._stopped: str | None = None
        self._index_uploaded = publisher is None
        self._finished = {"published", "gated"} if publisher else {"done", "published", "gated"}

    def run(self, options: ExportOptions) -> ExportSummary:
        lessons = self._lessons(options)
        for position, (ref, status) in enumerate(lessons, start=1):
            if self._stopped or self._should_stop(options, position):
                break
            if status in self._finished:
                continue
            progress = f"[{position}/{len(lessons)}]"
            if status != "done":
                status = self._export_lesson(ref, options, progress)
            if status == "done" and self._publisher is not None:
                self._publish_lesson(ref, options, progress)
        self._write_index(options)
        return self._summary()

    def _lessons(self, options: ExportOptions) -> list[tuple[LessonRef, LessonStatus]]:
        if options.rediscover or not self._state.lessons():
            refs = self._provider.discover(self._browser, options.course_url)
            self._state.record_discovery(refs)
            self._log(f"discovered {len(refs)} lessons")
        return self._state.lessons()

    def _should_stop(self, options: ExportOptions, position: int) -> bool:
        over_limit = options.limit is not None and position > options.limit
        return over_limit or self._enough_videos(options)

    def _enough_videos(self, options: ExportOptions) -> bool:
        return options.stop_after_videos is not None and self._videos_verified >= options.stop_after_videos

    def _export_lesson(self, ref: LessonRef, options: ExportOptions, progress: str) -> LessonStatus:
        try:
            lesson = self._provider.extract(self._browser, ref)
        except Exception as error:  # one broken lesson must not end a multi-hour run; the record keeps the reason
            return self._fail_lesson(ref, progress, "extract failed", error)
        if lesson.status == "gated":
            self._state.set_lesson(ref.lesson_id, "gated")
            self._log(f"{progress} {ref.title}: gated")
            return "gated"
        try:
            with self._reservation(lesson, options) as abort:
                files = self._export_assets(lesson, options, abort)
        except BudgetError as error:
            return self._fail_lesson(ref, progress, "over the staging budget", error)
        except media.Aborted as reason:
            self._stopped = str(reason)
            self._state.set_lesson(ref.lesson_id, "partial", f"stopped: {reason}")
            self._log(f"{progress} {ref.title}: stopped, staging budget: {reason}; rerun resumes here")
            return "partial"
        self._archive.write_lesson(options.out, lesson, files)
        finished = all(self._state.asset_status(ref.lesson_id, a.asset_id) in ("verified", "unsupported")
                       for a in lesson.assets)
        status: LessonStatus = "done" if finished else "partial"
        self._state.set_lesson(ref.lesson_id, status)
        self._log(f"{progress} {ref.title}: {status}, {len(files)}/{len(lesson.assets)} files")
        return status

    def _fail_lesson(self, ref: LessonRef, progress: str, what: str, error: BaseException) -> LessonStatus:
        self._state.set_lesson(ref.lesson_id, "failed", _reason(error))
        self._log(f"{progress} {ref.title}: {what}: {_reason(error)}")
        return "failed"

    @contextmanager
    def _reservation(self, lesson: Lesson, options: ExportOptions) -> Generator[Callable[[], str | None]]:
        """Yields the abort check every writer of this lesson polls; without a guard nothing ever aborts."""
        if self._space is None:
            yield lambda: None
            return
        pending = [a for a in lesson.assets
                   if self._state.asset_status(lesson.ref.lesson_id, a.asset_id) not in ("verified", "unsupported")]
        allowance = estimate_bytes(pending, options.max_height)
        lesson_dir = self._archive.lesson_dir(options.out, lesson.ref)
        already = _dir_bytes(lesson_dir)
        with self._space.reserve(allowance):
            yield self._space.watcher(lesson_dir, allowance + already)

    def _export_assets(self, lesson: Lesson, options: ExportOptions,
                       abort: Callable[[], str | None]) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for asset in lesson.assets:
            known = self._state.verified_path(lesson.ref.lesson_id, asset.asset_id)
            if known is not None and known.is_file():
                files[asset.asset_id] = known
                continue
            if self._enough_videos(options):
                continue
            path = self._export_asset(lesson.ref, asset, options, abort)
            if path is not None:
                files[asset.asset_id] = path
        return files

    def _export_asset(self, ref: LessonRef, asset: Asset, options: ExportOptions,
                      abort: Callable[[], str | None]) -> Path | None:
        """Raises media.Aborted when the budget stops this writer; its partial files are gone by then."""
        stem = self._archive.lesson_dir(options.out, ref) / _file_stem(asset)
        try:
            if reason := abort():
                raise media.Aborted(reason)
            source = self._provider.resolve(self._browser, ref, asset)
            path = self._fetch(source, asset, stem, options.max_height, abort=abort)
            verified = self._verify(path, asset, options, asset.expected_seconds or source.expected_seconds)
        except media.Aborted:
            _discard_partials(stem)
            raise
        except UnsupportedMedia as reason:
            self._state.mark_asset(ref.lesson_id, asset, "unsupported", _reason(reason))
            return None
        except Exception as error:  # recorded per asset; the rerun retries it
            self._state.mark_asset(ref.lesson_id, asset, "failed", _reason(error))
            return None
        self._state.mark_verified(ref.lesson_id, asset, verified)
        self._videos_verified += asset.kind == "video"
        return verified.path

    def _verify(self, path: Path, asset: Asset, options: ExportOptions, expected_seconds: float | None) -> VerifiedFile:
        try:
            return self._check(path, asset.kind, decode=options.decode, expected_seconds=expected_seconds)
        except media.VerificationError:
            media.set_aside(path)
            raise

    def _publish_lesson(self, ref: LessonRef, options: ExportOptions, progress: str) -> None:
        assert self._publisher is not None
        self._archive.write_index(options.out, self._state.lessons())
        lesson_dir = self._archive.lesson_dir(options.out, ref)
        try:
            self._publisher.publish_lesson(options.out, lesson_dir, self._state.verified_files(ref.lesson_id))
        except PublishError as error:
            self._log(f"{progress} {ref.title}: upload incomplete, retried next run: {error}")
            return
        self._state.set_lesson(ref.lesson_id, "published")
        self._log(f"{progress} {ref.title}: on Drive")

    def _write_index(self, options: ExportOptions) -> None:
        self._archive.write_index(options.out, self._state.lessons())
        if self._publisher is None:
            return
        try:
            self._publisher.publish_index(options.out)
            self._index_uploaded = True
        except PublishError as error:
            self._log(f"index upload failed: {error}")

    def _summary(self) -> ExportSummary:
        counts = self._state.counts()
        complete = (set(counts["lessons"]) <= self._finished and not counts["assets"].get("failed")
                    and self._index_uploaded)
        return ExportSummary(lessons=counts["lessons"], assets=counts["assets"], complete=complete and not self._stopped,
                             gaps=counts["assets"].get("unsupported", 0), stopped=self._stopped)


def estimate_bytes(assets: Sequence[Asset], max_height: int) -> int:
    """Staging a lesson needs: each video at the height cap's peak bitrate twice over (download plus merge), a flat
    allowance per file, and an overhead; a video without a stated length gets a fixed allowance."""
    bits_per_second = 2_500_000 if max_height <= 720 else 4_000_000
    total = LESSON_OVERHEAD_BYTES
    for asset in assets:
        if asset.kind != "video":
            total += FILE_BYTES
        elif asset.expected_seconds:
            total += int(asset.expected_seconds * bits_per_second / 8 * MERGE_FACTOR)
        else:
            total += UNKNOWN_VIDEO_BYTES
    return total


def _discard_partials(stem: Path) -> None:
    """Frees what a stopped writer left: every file of this asset (yt-dlp fragments, .part, a half merge)."""
    for leftover in stem.parent.glob(f"{stem.name}*"):
        if leftover.is_file() and not leftover.is_symlink():
            leftover.unlink(missing_ok=True)


def _dir_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(entry.stat().st_size for entry in directory.rglob("*") if entry.is_file() and not entry.is_symlink())


def _file_stem(asset: Asset) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in Path(asset.title).stem)[:60].strip("-.")
    return f"{asset.asset_id}-{safe}" if safe else asset.asset_id


def _reason(error: BaseException) -> str:
    return media.redact(f"{type(error).__name__}: {error}", limit=300)
