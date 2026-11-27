"""Reads a course's `.state.sqlite3` read-only for the videos the exporter has published.

It never goes through `CourseState`, whose constructor creates the schema: the transcriber must not write the
exporter's state, and a running export keeps its own writer.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

STATE_FILE = ".state.sqlite3"
PUBLISHED_VIDEOS = """
SELECT l.position, l.lesson_id, l.ref, a.asset_id, a.title, a.path, a.bytes, a.md5, a.duration
FROM lessons l JOIN assets a USING (lesson_id)
WHERE l.status = 'published' AND a.kind = 'video' AND a.status = 'verified'
ORDER BY l.position, a.asset_id
"""


@dataclass(frozen=True)
class VideoAsset:
    provider: str
    course: str
    position: int
    lesson_id: str
    lesson_title: str
    asset_id: str
    lesson_dir: str
    name: str
    bytes: int
    md5: str
    duration: float

    def drive_folder(self, remote: str) -> str:
        return f"{remote.rstrip('/')}/{self.course}/{self.lesson_dir}"


def published_videos(course_dir: Path) -> list[VideoAsset]:
    """Every verified video of a published lesson, in course order; `course_dir` is the exporter's `--out`."""
    with closing(_read_only(course_dir / STATE_FILE)) as db:
        rows = db.execute(PUBLISHED_VIDEOS).fetchall()
    return [_asset(course_dir, row) for row in rows]


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"no exporter state at {path}")
    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, timeout=30)


def _asset(course_dir: Path, row: tuple) -> VideoAsset:
    position, lesson_id, ref, asset_id, title, path, size, md5, duration = row
    staged = PurePosixPath(path)
    lesson_ref = json.loads(ref)
    return VideoAsset(
        provider=lesson_ref["provider"], course=course_dir.name, position=position, lesson_id=lesson_id,
        lesson_title=lesson_ref["title"], asset_id=asset_id,
        lesson_dir=staged.parent.relative_to(PurePosixPath(course_dir.as_posix())).as_posix(),
        name=staged.name, bytes=size, md5=md5.lower(), duration=duration,
    )
