"""Command line: `python3 -m course_exporter export|status|record`, one process per course output directory."""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import archive, config
from .browser import OpenClawBrowser
from .publish import GIB, DrivePublisher, SpaceBudget, SpaceGuard
from .runner import Exporter, ExportOptions
from .state import CourseState, VerifiedFile

PROVIDERS = {
    "kwiga": ("course_exporter.providers.kwiga", "KwigaProvider"),
    "getcourse": ("course_exporter.providers.getcourse", "GetCourseProvider"),
}
STATE_FILE = ".state.sqlite3"
LOCK_FILE = ".export.lock"
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_GAPS = 5
PILOT_LESSON_LIMIT = 5


def main(argv: list[str] | None = None) -> int:
    settings = config.load()
    args = _parser(settings).parse_args(argv)
    args.settings = settings
    return args.command(args)


def _parser(settings: config.Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="course_exporter")
    commands = parser.add_subparsers(required=True)
    export = commands.add_parser("export", help="export a course lesson by lesson; reruns resume")
    export.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    export.add_argument("--course", required=True, help="course URL")
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--max-height", type=int, default=720)
    export.add_argument("--limit", type=int, help="stop after this many lessons")
    export.add_argument("--first-video", action="store_true",
                        help="stop after the first verified video (pilot); without --limit it reads at most 5 lessons")
    export.add_argument("--rediscover", action="store_true", help="read the course tree again")
    export.add_argument("--decode", action="store_true", help="fully decode every video while verifying")
    export.add_argument("--refresh-pages", action="store_true",
                        help="re-extract published lessons; files already on Drive are linked, not fetched again")
    export.add_argument("--name", help="course folder name on Drive; with drive_remote set it is <drive_remote>/<name>")
    export.add_argument("--drive", help="full rclone remote path; overrides drive_remote/<name>")
    export.add_argument("--staging-root", type=Path, default=_path(settings.staging_root),
                        help="directory whose disk and size the space guard watches (config: staging_root)")
    export.add_argument("--min-free-gib", type=int, default=settings.min_free_gib,
                        help="free space kept for other jobs on the staging disk")
    export.add_argument("--max-staging-gib", type=int, default=settings.max_staging_gib)
    export.set_defaults(command=_export)
    status = commands.add_parser("status", help="lesson and asset counts of an output directory")
    status.add_argument("--out", required=True, type=Path)
    status.set_defaults(command=_status)
    record = commands.add_parser("record", help="mark a known asset verified with a file fetched outside the exporter "
                                                "and already placed in the lesson's Drive folder")
    record.add_argument("--out", required=True, type=Path)
    record.add_argument("--lesson", required=True, help="lesson id")
    record.add_argument("--asset", required=True, help="asset id")
    record.add_argument("--name", required=True, help="file name inside the lesson folder")
    record.add_argument("--bytes", required=True, type=int)
    record.add_argument("--md5", required=True)
    record.add_argument("--sha256", default="")
    record.set_defaults(command=_record)
    return parser


def _export(args: argparse.Namespace) -> int:
    if args.staging_root is not None and not args.out.resolve().is_relative_to(args.staging_root.resolve()):
        print(f"--out must be inside --staging-root {args.staging_root}", file=sys.stderr)
        return EXIT_USAGE
    args.out.mkdir(parents=True, exist_ok=True)
    with _exclusive(args.out / LOCK_FILE) as acquired:
        if not acquired:
            print(f"another export holds {args.out / LOCK_FILE}", file=sys.stderr)
            return EXIT_BUSY
        state = CourseState(args.out / STATE_FILE)
        settings: config.Settings = args.settings
        browser = OpenClawBrowser(settings.browser_node, settings.browser_profile, settings.openclaw)
        drive = _drive_target(args, settings)
        try:
            exporter = Exporter(_provider(args.provider), browser, state, archive, log=_log,
                                publisher=DrivePublisher(drive) if drive else None,
                                space=_space_guard(args))
            summary = exporter.run(ExportOptions(
                course_url=args.course, out=args.out, max_height=args.max_height,
                limit=args.limit if args.limit is not None or not args.first_video else PILOT_LESSON_LIMIT,
                stop_after_videos=1 if args.first_video else None, rediscover=args.rediscover, decode=args.decode,
                refresh_pages=args.refresh_pages))
        finally:
            browser.close()
            state.close()
    print(json.dumps({"lessons": summary.lessons, "assets": summary.assets, "complete": summary.complete,
                      "gaps": summary.gaps, "stopped": summary.stopped}))
    if not summary.complete:
        return EXIT_INCOMPLETE
    return EXIT_GAPS if summary.gaps else 0


def _status(args: argparse.Namespace) -> int:
    state = CourseState(args.out / STATE_FILE)
    try:
        print(json.dumps(state.counts()))
        for record in state.assets():
            if record.status in ("failed", "unsupported"):
                print(f"{record.status}\t{record.lesson_id}\t{record.asset_id}\t{record.detail}")
    finally:
        state.close()
    return 0


def _record(args: argparse.Namespace) -> int:
    if "/" in args.name or args.name in ("", ".", ".."):
        print("--name must be a plain file name", file=sys.stderr)
        return EXIT_USAGE
    with _exclusive(args.out / LOCK_FILE) as acquired:
        if not acquired:
            print(f"another export holds {args.out / LOCK_FILE}", file=sys.stderr)
            return EXIT_BUSY
        state = CourseState(args.out / STATE_FILE)
        try:
            refs = {ref.lesson_id: ref for ref, _ in state.lessons()}
            if args.lesson not in refs:
                print(f"no lesson {args.lesson} in {args.out}", file=sys.stderr)
                return EXIT_USAGE
            path = archive.lesson_dir(args.out, refs[args.lesson]) / args.name
            state.record_file(args.lesson, args.asset, VerifiedFile(path, args.bytes, args.sha256, md5=args.md5.lower()))
        except KeyError as error:
            print(error.args[0], file=sys.stderr)
            return EXIT_USAGE
        finally:
            state.close()
    return 0


def _drive_target(args: argparse.Namespace, settings: config.Settings) -> str | None:
    if args.drive:
        return args.drive
    if settings.drive_remote and args.name:
        return f"{settings.drive_remote.rstrip('/')}/{args.name}"
    return None


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _space_guard(args: argparse.Namespace) -> SpaceGuard | None:
    if args.staging_root is None:
        return None
    budget = SpaceBudget(args.staging_root, min_free=args.min_free_gib * GIB, max_staging=args.max_staging_gib * GIB)
    return SpaceGuard(budget, log=_log)


def _log(line: str) -> None:
    print(line, flush=True)


def _provider(name: str):
    module, cls = PROVIDERS[name]
    return getattr(importlib.import_module(module), cls)()


@contextmanager
def _exclusive(path: Path) -> Iterator[bool]:
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
