"""Per-course SQLite record of what was discovered, extracted, downloaded and verified, so a rerun resumes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .model import Asset, LessonRef

LessonStatus = Literal["discovered", "gated", "done", "partial", "failed", "published"]
AssetStatus = Literal["pending", "verified", "failed", "unsupported"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
  lesson_id TEXT PRIMARY KEY, position INTEGER NOT NULL, ref TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  lesson_id TEXT NOT NULL, asset_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  status TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '', md5 TEXT NOT NULL DEFAULT '', duration REAL NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, PRIMARY KEY (lesson_id, asset_id)
);
"""
MIGRATIONS = (
    ("lessons", "published", "ALTER TABLE lessons ADD COLUMN published INTEGER NOT NULL DEFAULT 0;"
                             " UPDATE lessons SET published = 1 WHERE status = 'published';"),
)


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    bytes: int
    sha256: str
    duration: float = 0.0
    md5: str = ""


@dataclass(frozen=True)
class AssetRecord:
    lesson_id: str
    asset_id: str
    kind: str
    title: str
    status: AssetStatus
    path: str
    detail: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ref_to_json(ref: LessonRef) -> str:
    return json.dumps({**ref.__dict__, "order": list(ref.order), "modules": list(ref.modules)}, ensure_ascii=False)


def _ref_from_json(text: str) -> LessonRef:
    data = json.loads(text)
    return LessonRef(**{**data, "order": tuple(data["order"]), "modules": tuple(data["modules"])})


class CourseState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def record_discovery(self, refs: list[LessonRef]) -> None:
        """Stores the course order; a lesson already seen keeps its status, except a gated one, which the next
        extract checks again because the owner may have unlocked it since."""
        with self._db:
            for position, ref in enumerate(refs):
                self._db.execute(
                    "INSERT INTO lessons (lesson_id, position, ref, status, updated_at) VALUES (?, ?, ?, 'discovered', ?)"
                    " ON CONFLICT(lesson_id) DO UPDATE SET position = excluded.position, ref = excluded.ref,"
                    " status = CASE WHEN lessons.status = 'gated' THEN 'discovered' ELSE lessons.status END",
                    (ref.lesson_id, position, _ref_to_json(ref), _now()),
                )

    def lessons(self) -> list[tuple[LessonRef, LessonStatus]]:
        rows = self._db.execute("SELECT ref, status FROM lessons ORDER BY position").fetchall()
        return [(_ref_from_json(ref), status) for ref, status in rows]

    def set_lesson(self, lesson_id: str, status: LessonStatus, detail: str = "") -> None:
        with self._db:
            self._db.execute(
                "UPDATE lessons SET status = ?, detail = ?, updated_at = ?, published = published OR ?"
                " WHERE lesson_id = ?",
                (status, detail, _now(), status == "published", lesson_id),
            )

    def was_published(self, lesson_id: str) -> bool:
        """True once the lesson reached Drive: its verified files live there even after a later re-extract fails, so
        a staged file that is gone counts as uploaded, not lost."""
        row = self._db.execute("SELECT published FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
        return bool(row and row[0])

    def asset_status(self, lesson_id: str, asset_id: str) -> AssetStatus | None:
        row = self._db.execute(
            "SELECT status FROM assets WHERE lesson_id = ? AND asset_id = ?", (lesson_id, asset_id)
        ).fetchone()
        return row[0] if row else None

    def verified_path(self, lesson_id: str, asset_id: str) -> Path | None:
        row = self._db.execute(
            "SELECT path FROM assets WHERE lesson_id = ? AND asset_id = ? AND status = 'verified'", (lesson_id, asset_id)
        ).fetchone()
        return Path(row[0]) if row else None

    def verified_files(self, lesson_id: str) -> list[VerifiedFile]:
        rows = self._db.execute(
            "SELECT path, bytes, sha256, duration, md5 FROM assets WHERE lesson_id = ? AND status = 'verified'"
            " ORDER BY asset_id", (lesson_id,)
        ).fetchall()
        return [VerifiedFile(Path(path), size, sha256, duration, md5) for path, size, sha256, duration, md5 in rows]

    def mark_verified(self, lesson_id: str, asset: Asset, file: VerifiedFile) -> None:
        self._upsert_asset(lesson_id, asset, "verified", str(file.path), file.bytes, file.sha256, file.duration, "",
                           file.md5)

    def mark_asset(self, lesson_id: str, asset: Asset, status: AssetStatus, detail: str) -> None:
        self._upsert_asset(lesson_id, asset, status, "", 0, "", 0.0, detail, "")

    def record_file(self, lesson_id: str, asset_id: str, file: VerifiedFile) -> None:
        """Marks an asset the exporter already knows as verified with a file obtained outside it (fetched by hand in
        the owner's browser); raises KeyError for an asset the state has never seen."""
        with self._db:
            updated = self._db.execute(
                "UPDATE assets SET status = 'verified', path = ?, bytes = ?, sha256 = ?, md5 = ?, duration = ?,"
                " detail = 'recorded by hand', updated_at = ? WHERE lesson_id = ? AND asset_id = ?",
                (str(file.path), file.bytes, file.sha256, file.md5, file.duration, _now(), lesson_id, asset_id),
            ).rowcount
        if not updated:
            raise KeyError(f"no asset {asset_id} in lesson {lesson_id}")

    def assets(self) -> list[AssetRecord]:
        rows = self._db.execute(
            "SELECT a.lesson_id, a.asset_id, a.kind, a.title, a.status, a.path, a.detail FROM assets a"
            " JOIN lessons l USING (lesson_id) ORDER BY l.position, a.asset_id"
        ).fetchall()
        return [AssetRecord(*row) for row in rows]

    def counts(self) -> dict[str, dict[str, int]]:
        lessons = dict(self._db.execute("SELECT status, COUNT(*) FROM lessons GROUP BY status").fetchall())
        assets = dict(self._db.execute("SELECT status, COUNT(*) FROM assets GROUP BY status").fetchall())
        return {"lessons": lessons, "assets": assets}

    def _migrate(self) -> None:
        for table, column, script in MIGRATIONS:
            columns = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                with self._db:
                    self._db.executescript(script)

    def _upsert_asset(self, lesson_id: str, asset: Asset, status: AssetStatus, path: str, size: int, sha256: str,
                      duration: float, detail: str, md5: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO assets (lesson_id, asset_id, kind, title, status, path, bytes, sha256, md5, duration,"
                " detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(lesson_id, asset_id)"
                " DO UPDATE SET status = excluded.status, path = excluded.path, bytes = excluded.bytes,"
                " sha256 = excluded.sha256, md5 = excluded.md5, duration = excluded.duration,"
                " detail = excluded.detail, updated_at = excluded.updated_at",
                (lesson_id, asset.asset_id, asset.kind, asset.title, status, path, size, sha256, md5, duration, detail,
                 _now()),
            )
