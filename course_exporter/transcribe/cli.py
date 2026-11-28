"""Operator commands for the VPS journal and the on-demand service."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, d2_pins, prepare, source
from .journal import Journal
from .publisher import FOLDER, PublishError, Publisher, Writer

HOME = Path.home()
CONFIG_PATH = HOME / ".config/uch-transcribe/config.toml"
PUBLISHER_RCLONE = HOME / ".config/uch-transcribe/rclone.conf"
JOURNAL_PATH = HOME / ".local/state/course-exporter/transcribe/journal.sqlite3"
CACHE_DIR = HOME / ".cache/course-transcribe"
PUBLISHER_REMOTE = "courses-publisher"
LANGUAGES = {"ru", "uk"}


@dataclass(frozen=True)
class Course:
    directory: Path
    language: str


@dataclass(frozen=True)
class Settings:
    staging_root: Path
    source_remote: str
    courses_root_id: str
    courses: dict[str, Course]

    @classmethod
    def load(cls, path: Path) -> Settings:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        staging = Path(raw["staging_root"]).resolve()
        if not Path(raw["staging_root"]).is_absolute() or not staging.is_dir():
            raise ValueError("staging_root must be an existing absolute directory")
        remote = raw["source_remote"]
        if not isinstance(remote, str) or not remote.endswith(":Courses") or remote == ":Courses":
            raise ValueError("source_remote must name the Courses folder of the source identity")
        courses = {}
        for key, language in raw["courses"].items():
            directory = staging / key
            if (Path(key).name != key or key in (".", "..") or language not in LANGUAGES
                    or directory.resolve() != directory or not directory.is_dir()):
                raise ValueError("courses must map direct staging directories to ru or uk")
            courses[key] = Course(directory, language)
        if not courses:
            raise ValueError("at least one course is required")
        return cls(staging, remote, source._file_id(raw["courses_root_id"]), courses)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="VPS transcriber settings")
    commands = parser.add_subparsers(dest="transcribe_command", required=True)
    scan = commands.add_parser("reconcile", help="observe current published Drive revisions")
    scan.add_argument("--closing", action="store_true", help="mark newly observed revisions as new")
    commands.add_parser("candidates", help="list pilot candidates without a valid selection")
    pilot = commands.add_parser("pilot", help="set pilot scope to two exact revision job IDs")
    pilot.add_argument("job_ids", nargs=2)
    commands.add_parser("bulk", help="open bulk scope after both pilot revisions are verified")
    commands.add_parser("status", help="show scope, revision states and reasons")
    commands.add_parser("prepare", help="drain VPS work from the on-demand service")
    parser.set_defaults(command=dispatch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="course_exporter transcribe")
    configure(parser)
    return dispatch(parser.parse_args(argv))


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def _reader(settings: Settings) -> source.DriveApi:
    return source.DriveApi(source.RcloneToken(settings.source_remote))


def _publisher(settings: Settings, journal: Journal, reader: source.DriveApi) -> Publisher:
    def still_published(job) -> bool:
        course = settings.courses.get(job.asset.course)
        return bool(course and catalog.is_published(course.directory, job.asset))

    writer = Writer(source.RcloneToken(PUBLISHER_REMOTE, config_path=PUBLISHER_RCLONE))
    return Publisher(journal, writer, reader, settings.courses_root_id, still_published)


def reconcile(settings: Settings, journal: Journal, reader: source.DriveApi, provenance: str) -> dict:
    listing = {}

    def memoized(argv: list[str], **kwargs):
        key = tuple(argv)
        if key not in listing:
            listing[key] = subprocess.run(argv, **kwargs)
        return listing[key]

    observed, unavailable = [], []
    for name, course in settings.courses.items():
        for asset in catalog.published_videos(course.directory):
            try:
                video = source.resolve(asset, settings.source_remote, reader, run=memoized)
            except (source.SourceError, subprocess.TimeoutExpired) as exc:
                journal.record_unavailable(asset, str(exc))
                unavailable.append({"course": name, "lesson": asset.lesson_id, "asset": asset.asset_id,
                                    "md5": asset.md5, "state": "replaced/unavailable", "reason": str(exc)})
                continue
            observed.append(journal.observe(asset, video, course.language, d2_pins.PRODUCER_CONFIG, provenance))
    return {"observed": observed, "unavailable": unavailable}


def _status(journal: Journal) -> dict:
    rows = [{
        "job_id": job.id, "provider": job.asset.provider, "course": job.asset.course, "lesson": job.asset.lesson_id,
        "asset": job.asset.asset_id, "drive_file_id": job.source.id, "revision": job.source.revision,
        "md5": job.source.md5, "state": job.state, "reason": job.reason, "language": job.language,
        "observed": datetime.fromtimestamp(job.observed, timezone.utc).isoformat(), "provenance": job.provenance,
    } for job in journal.report()]
    return {**journal.status(), "items": rows, "unavailable": journal.unavailable()}


def dispatch(args: argparse.Namespace) -> int:
    try:
        journal = Journal(JOURNAL_PATH)
        action = args.transcribe_command
        if action == "status":
            _print(_status(journal))
            return 0
        if action == "bulk":
            journal.open_bulk()
            _print(journal.status())
            return 0
        settings = Settings.load(args.config)
        reader = _reader(settings)
        if action == "reconcile":
            result = reconcile(settings, journal, reader, "new" if args.closing else "backlog")
            _print(result)
            return int(bool(result["unavailable"]))
        publisher = _publisher(settings, journal, reader)
        if action == "prepare":
            CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            def check_headroom(size: int) -> None:
                prepare.check_headroom(settings.staging_root, CACHE_DIR, size)

            cache = source.AudioCache(CACHE_DIR, reader, headroom=check_headroom)
            prepare.run(journal, cache, publisher, lambda: prepare.admission(HOME, settings.staging_root, CACHE_DIR))
            return 0
        if action in ("candidates", "pilot"):
            root = publisher.writer.metadata(settings.courses_root_id)
            if not root or root.get("name") != "Courses" or root.get("mimeType") != FOLDER or root.get("trashed"):
                raise ValueError("publisher cannot verify the configured Courses root")
            jobs = journal.report(scoped=False)
            candidates = []
            for job in sorted(jobs, key=lambda row: (row.language, row.asset.duration, row.id)):
                if job.state != "queued" or (action == "pilot" and job.id not in args.job_ids):
                    continue
                try:
                    compliant = publisher.already_compliant(job)
                except source.SourceChanged:
                    if action == "pilot":
                        raise
                    continue
                if not compliant:
                    candidates.append({"job_id": job.id, "language": job.language, "asset": asdict(job.asset),
                                       "source": asdict(job.source)})
            if action == "candidates":
                _print(candidates)
            else:
                if {item["job_id"] for item in candidates} != set(args.job_ids):
                    raise ValueError("pilot requires two current revisions without valid selections")
                journal.set_pilot(args.job_ids)
                _print(journal.status())
            return 0
        raise ValueError("unknown transcribe action")
    except (ValueError, KeyError, OSError, source.SourceError, PublishError, subprocess.SubprocessError) as exc:
        # Config and Drive error messages are local and do not contain OAuth responses.
        print(f"transcribe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
