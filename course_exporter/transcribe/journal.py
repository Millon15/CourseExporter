"""Durable revision, lease, receipt and publish-plan state on the VPS."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .catalog import VideoAsset
from .source import CachedAudio, DriveFile

LEASE_SECONDS = 600
MAX_EXPIRED_LEASES = 3
MAX_PREPARE_ATTEMPTS = 3
TERMINAL = ("verified", "already compliant", "failed", "replaced/unavailable")
IN_SCOPE = "(scope.mode = 'bulk' OR (scope.mode = 'pilot' AND j.id IN (SELECT job_id FROM pilot)))"
SCHEMA = """
CREATE TABLE IF NOT EXISTS scope (id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL);
INSERT OR IGNORE INTO scope VALUES (1, 'closed');
CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY CHECK (id = 1), paused TEXT);
INSERT OR IGNORE INTO runtime VALUES (1, NULL);
CREATE TABLE IF NOT EXISTS pilot (job_id TEXT PRIMARY KEY REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, asset TEXT NOT NULL, source TEXT NOT NULL,
    language TEXT NOT NULL, producer_config TEXT NOT NULL,
    observed REAL NOT NULL, provenance TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT,
    audio_path TEXT, audio_sha256 TEXT, audio_bytes INTEGER, duration_s REAL,
    lease_id TEXT UNIQUE, expires REAL, expired_leases INTEGER DEFAULT 0 NOT NULL,
    result BLOB, result_sha256 TEXT, receipt_id TEXT,
    publish_attempts INTEGER DEFAULT 0 NOT NULL, interrupted_prepares INTEGER DEFAULT 0 NOT NULL
);
CREATE TABLE IF NOT EXISTS unavailable (
    course TEXT NOT NULL, lesson TEXT NOT NULL, asset TEXT NOT NULL, md5 TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(course,lesson,asset,md5)
);
CREATE TABLE IF NOT EXISTS plans (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    destination TEXT NOT NULL, generation INTEGER NOT NULL,
    UNIQUE (destination, generation)
);
CREATE TABLE IF NOT EXISTS files (
    job_id TEXT NOT NULL REFERENCES plans(job_id), position INTEGER NOT NULL,
    role TEXT NOT NULL, id TEXT NOT NULL UNIQUE, parent TEXT NOT NULL,
    name TEXT NOT NULL, data BLOB NOT NULL, sha256 TEXT NOT NULL,
    PRIMARY KEY (job_id, position)
);
"""


class LeaseRefused(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    id: str
    asset: VideoAsset
    source: DriveFile
    language: str
    producer_config: str
    observed: float
    provenance: str
    state: str
    reason: str | None
    audio_path: str | None
    audio_sha256: str | None
    audio_bytes: int | None
    duration_s: float | None
    lease_id: str | None
    expires: float | None
    publish_attempts: int
    expired_leases: int


@dataclass(frozen=True)
class PlannedFile:
    role: str
    id: str
    parent: str
    name: str
    data: bytes
    sha256: str


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _job(row: sqlite3.Row) -> Job:
    data = {key: row[key] for key in Job.__dataclass_fields__}
    data["asset"] = VideoAsset(**json.loads(data["asset"]))
    source = json.loads(data["source"])
    source["parents"] = tuple(source["parents"])
    data["source"] = DriveFile(**source)
    return Job(**data)


class Journal:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if path.stat().st_mode & 0o077:
            raise ValueError("journal must be private")
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def observe(self, asset: VideoAsset, source: DriveFile, language: str, producer: str, provenance: str) -> str:
        identity = (asset.provider, asset.course, asset.lesson_id, asset.asset_id, source.id,
                    source.revision, source.md5, producer)
        job_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with self._connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO jobs (id,asset,source,language,producer_config,observed,provenance,state) "
                "VALUES (?,?,?,?,?,?,?,'queued')",
                (job_id, json.dumps(asdict(asset)), json.dumps(asdict(source)), language, producer,
                 self.clock(), provenance),
            )
            db.execute("DELETE FROM unavailable WHERE course=? AND lesson=? AND asset=? AND md5=?",
                       (asset.course, asset.lesson_id, asset.asset_id, asset.md5))
            db.execute(
                "UPDATE jobs SET state=CASE WHEN result IS NULL THEN 'queued' ELSE 'received' END,reason=NULL "
                "WHERE id=? AND state='replaced/unavailable'",
                (job_id,),
            )
        return job_id

    def record_unavailable(self, asset: VideoAsset, reason: str) -> None:
        with self._connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO unavailable VALUES (?,?,?,?,?)",
                       (asset.course, asset.lesson_id, asset.asset_id, asset.md5, reason))

    def unavailable(self) -> list[dict]:
        with self._connect() as db:
            if self._scope(db) != "bulk":
                return []
            rows = db.execute("SELECT * FROM unavailable ORDER BY course,lesson,asset,md5").fetchall()
            return [{**dict(row), "state": "replaced/unavailable"} for row in rows]

    def set_pilot(self, job_ids: list[str]) -> None:
        if len(set(job_ids)) != 2:
            raise ValueError("pilot requires exactly two distinct revision job IDs")
        with self._connect(write=True) as db:
            rows = db.execute("SELECT language,state FROM jobs WHERE id IN (?,?)", job_ids).fetchall()
            if {row["language"] for row in rows} != {"ru", "uk"} or any(row["state"] != "queued" for row in rows):
                raise ValueError("pilot requires one queued Russian and one queued Ukrainian revision")
            if self._scope(db) != "closed":
                raise ValueError("pilot scope was already set")
            db.executemany("INSERT INTO pilot VALUES (?)", [(job_id,) for job_id in job_ids])
            db.execute("UPDATE scope SET mode='pilot'")

    def open_bulk(self) -> None:
        with self._connect(write=True) as db:
            if self._scope(db) == "closed" or db.execute(
                "SELECT count(*) FROM pilot JOIN jobs ON jobs.id=pilot.job_id WHERE state='verified'"
            ).fetchone()[0] != 2:
                raise ValueError("bulk requires two verified pilot jobs")
            db.execute("UPDATE scope SET mode='bulk'")

    @staticmethod
    def _scope(db: sqlite3.Connection) -> str:
        return db.execute("SELECT mode FROM scope").fetchone()[0]

    def status(self) -> dict:
        with self._connect() as db:
            counts = db.execute(
                f"SELECT state,count(*) AS n FROM jobs j, scope WHERE {IN_SCOPE} GROUP BY state"
            ).fetchall()
            return {"counts": {row["state"]: row["n"] for row in counts}, "scope": self._scope(db)}

    def report(self, scoped: bool = True) -> list[Job]:
        with self._connect() as db:
            condition = f"WHERE {IN_SCOPE}" if scoped else ""
            rows = db.execute(f"SELECT j.* FROM jobs j, scope {condition} ORDER BY observed,id").fetchall()
            return [_job(row) for row in rows]

    def pause(self, reason: str | None) -> None:
        with self._connect(write=True) as db:
            db.execute("UPDATE runtime SET paused=?", (reason,))

    def request(self) -> dict:
        with self._connect(write=True) as db:
            self._expire(db)
            counts = db.execute(
                f"SELECT j.state,j.reason FROM jobs j,scope WHERE {IN_SCOPE} AND j.state IN "
                "('queued','preparing','deferred','ready','leased','received','publishing') ORDER BY j.observed,j.id"
            ).fetchall()
            paused = db.execute("SELECT paused FROM runtime").fetchone()[0]
        states = {row["state"] for row in counts}
        if "ready" in states and "leased" not in states:
            return {"state": "ready", "reason": None, "retry_after_s": None}
        if paused and counts:
            return {"state": "deferred", "reason": paused, "retry_after_s": 60}
        if states & {"queued", "preparing", "leased", "received", "publishing"}:
            return {"state": "preparing", "reason": None, "retry_after_s": 5}
        if counts:
            return {"state": "deferred", "reason": counts[0]["reason"], "retry_after_s": 60}
        return {"state": "empty", "reason": None, "retry_after_s": None}

    def next_prepare(self) -> Job | None:
        with self._connect(write=True) as db:
            self._expire(db)
            # The exclusive service lock proves a surviving preparing row was interrupted.
            db.execute(
                "UPDATE jobs SET state=CASE WHEN interrupted_prepares+1>=? THEN 'failed' ELSE 'queued' END,"
                " reason=CASE WHEN interrupted_prepares+1>=? THEN 'preparation: interrupted three times' ELSE NULL END,"
                " interrupted_prepares=interrupted_prepares+1 WHERE state='preparing'",
                (MAX_PREPARE_ATTEMPTS, MAX_PREPARE_ATTEMPTS),
            )
            if self._find(db, "j.state='ready'"):
                return None
            row = self._find(db, "j.state IN ('preparing','queued','deferred')",
                             order="CASE j.state WHEN 'preparing' THEN 0 ELSE 1 END,j.observed,j.id")
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='preparing',reason=NULL WHERE id=?", (row["id"],))
            return _job(row)

    def ready(self, job_id: str, audio: CachedAudio) -> None:
        with self._connect(write=True) as db:
            self._require_state(db, job_id, "preparing")
            db.execute(
                "UPDATE jobs SET state='ready',audio_path=?,audio_sha256=?,audio_bytes=?,duration_s=? WHERE id=?",
                (str(audio.path), audio.sha256, audio.path.stat().st_size, audio.seconds, job_id),
            )

    def mark(self, job_id: str, expected: str, state: str, reason: str | None) -> None:
        if state not in (*TERMINAL, "deferred", "queued"):
            raise ValueError("invalid final or retry state")
        with self._connect(write=True) as db:
            self._require_state(db, job_id, expected)
            db.execute("UPDATE jobs SET state=?,reason=? WHERE id=?", (state, reason, job_id))

    def lease(self) -> dict | None:
        with self._connect(write=True) as db:
            self._expire(db)
            if self._find(db, "j.state='leased'"):
                return None
            row = self._find(db, "j.state='ready'")
            if row is None:
                return None
            lease_id, expires = secrets.token_hex(32), self.clock() + LEASE_SECONDS
            db.execute("UPDATE jobs SET state='leased',lease_id=?,expires=? WHERE id=?",
                       (lease_id, expires, row["id"]))
            return {"lease_id": lease_id, "job_id": row["id"], "expires_at": _utc(expires),
                    **{name: row[name] for name in ("audio_sha256", "audio_bytes", "duration_s",
                                                      "language", "producer_config")}}

    def renew(self, lease_id: str) -> dict:
        with self._connect(write=True) as db:
            self._live_lease(db, lease_id)
            expires = self.clock() + LEASE_SECONDS
            db.execute("UPDATE jobs SET expires=? WHERE lease_id=?", (expires, lease_id))
        return {"lease_id": lease_id, "expires_at": _utc(expires)}

    def leased_job(self, lease_id: str) -> Job:
        with self._connect() as db:
            return _job(self._live_lease(db, lease_id))

    def accept(self, lease_id: str, data: bytes, validate: Callable[[Job, bytes], None]) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE lease_id=?", (lease_id,)).fetchone()
            if row is not None and row["receipt_id"]:
                return self._repeat(row, digest)
            job = _job(self._live_lease(db, lease_id))
        try:
            validate(job, data)
        except ValueError as exc:
            with self._connect(write=True) as db:
                row = db.execute("SELECT * FROM jobs WHERE lease_id=?", (lease_id,)).fetchone()
                if row is not None and row["receipt_id"]:
                    return self._repeat(row, digest)
                row = self._live_lease(db, lease_id)
                db.execute("UPDATE jobs SET state='failed',reason=? WHERE id=?",
                           (f"validation: {exc}", row["id"]))
            raise
        with self._connect(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE lease_id=?", (lease_id,)).fetchone()
            if row is not None and row["receipt_id"]:
                return self._repeat(row, digest)
            row = self._live_lease(db, lease_id)
            receipt_id = secrets.token_hex(16)
            db.execute("UPDATE jobs SET state='received',result=?,result_sha256=?,receipt_id=? WHERE id=?",
                       (data, digest, receipt_id, row["id"]))
            return self._receipt(receipt_id, row["id"], digest)

    def _repeat(self, row: sqlite3.Row, digest: str) -> dict:
        if row["result_sha256"] != digest:
            raise LeaseRefused("a different result was already accepted for this lease")
        return self._receipt(row["receipt_id"], row["id"], digest)

    @staticmethod
    def _receipt(receipt_id: str, job_id: str, digest: str) -> dict:
        return {"receipt_id": receipt_id, "job_id": job_id, "result_sha256": digest, "state": "received"}

    def _expire(self, db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE jobs SET state=CASE WHEN expired_leases+1>=? THEN 'failed' ELSE 'ready' END,"
            " reason=CASE WHEN expired_leases+1>=? THEN 'compute: three leases expired without a result' ELSE NULL END,"
            " lease_id=NULL,expires=NULL,expired_leases=expired_leases+1 WHERE state='leased' AND expires<=?",
            (MAX_EXPIRED_LEASES, MAX_EXPIRED_LEASES, self.clock()),
        )

    def _live_lease(self, db: sqlite3.Connection, lease_id: str) -> sqlite3.Row:
        row = self._find(db, "j.lease_id=? AND j.state='leased' AND j.expires>?", (lease_id, self.clock()))
        if row is None:
            raise LeaseRefused("lease is unknown, expired or fenced")
        return row

    @staticmethod
    def _find(db: sqlite3.Connection, condition: str, params: tuple = (), order: str = "j.observed,j.id"
              ) -> sqlite3.Row | None:
        return db.execute(f"SELECT j.* FROM jobs j,scope WHERE {IN_SCOPE} AND ({condition}) ORDER BY {order} LIMIT 1",
                          params).fetchone()

    def _require_state(self, db: sqlite3.Connection, job_id: str, state: str) -> sqlite3.Row:
        row = self._find(db, "j.id=? AND j.state=?", (job_id, state))
        if row is None:
            raise ValueError(f"job is outside scope or does not have state {state}")
        return row

    def next_publish(self) -> tuple[Job, bytes] | None:
        with self._connect(write=True) as db:
            row = self._find(db, "j.state IN ('received','publishing')")
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='publishing',publish_attempts=publish_attempts+1 WHERE id=?",
                       (row["id"],))
            job = _job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            return job, row["result"]

    def highest_generation(self, destination: str) -> int:
        with self._connect() as db:
            return db.execute("SELECT coalesce(max(generation),0) FROM plans WHERE destination=?",
                              (destination,)).fetchone()[0]

    def save_plan(self, job_id: str, destination: str, generation: int, files: list[PlannedFile]) -> None:
        with self._connect(write=True) as db:
            self._require_state(db, job_id, "publishing")
            db.execute("INSERT INTO plans VALUES (?,?,?)", (job_id, destination, generation))
            db.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", [
                (job_id, position, file.role, file.id, file.parent, file.name, file.data, file.sha256)
                for position, file in enumerate(files)
            ])

    def plan(self, job_id: str) -> tuple[int, list[PlannedFile]] | None:
        with self._connect() as db:
            header = db.execute("SELECT generation FROM plans WHERE job_id=?", (job_id,)).fetchone()
            if header is None:
                return None
            rows = db.execute("SELECT role,id,parent,name,data,sha256 FROM files WHERE job_id=? ORDER BY position",
                              (job_id,)).fetchall()
            return header["generation"], [PlannedFile(**dict(row)) for row in rows]
