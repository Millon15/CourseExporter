"""The dedicated Mac key can call six fixed operations over standard streams."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Callable, TextIO

from . import d2_pins, segments
from .journal import Job, Journal, LeaseRefused

MAX_RESULT_BYTES = 16 * 1024 * 1024
ARITY = {"request": 0, "lease": 0, "audio": 1, "renew": 1, "submit": 1, "status": 0}
ID = re.compile(r"[0-9a-f]{64}")


class Refusal(ValueError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def parse(command: str) -> tuple[str, list[str]]:
    if len(command) > 256 or not re.fullmatch(r"[a-z0-9 ]+", command):
        raise Refusal(2, "command is outside the fixed verb syntax")
    argv = shlex.split(command)
    if not argv or ARITY.get(argv[0]) != len(argv) - 1 or any(not ID.fullmatch(arg) for arg in argv[1:]):
        raise Refusal(2, "verb or argument is outside the allowlist")
    return argv[0], argv[1:]


def _unique_object(pairs: list[tuple]) -> dict:
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate key in result")
        output[key] = value
    return output


def _number(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite number in result")
    return value


def _validate(job: Job, data: bytes) -> None:
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_number,
                              parse_float=_number)
        segments.validate(document)
        # UTF-16 surrogate escapes are legal JSON syntax but cannot be published as UTF-8.
        json.dumps(document, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValueError("result is not a valid uch-segments/1 document") from None
    if document["pipeline"] != "D2" or type(document.get("result_version")) is not int or (
        document["result_version"] != d2_pins.RESULT_VERSION
    ):
        raise ValueError("unexpected result pipeline or version")
    source = document["source"]
    if (source["clip_id"], source["audio_sha256"], source["offset_s"],
            document["language"], document["producer_config"]) != (
                job.id, job.audio_sha256, 0, job.language, job.producer_config):
        raise ValueError("result does not match the leased job, audio, offset, language or configuration")
    if abs(source["duration_s"] - job.duration_s) > 0.5:
        raise ValueError("result duration differs from the leased audio")
    for cue in document["segments"]:
        if cue["end"] > job.duration_s + 0.5 or any(word["end"] > job.duration_s + 0.5 for word in cue["words"]):
            raise ValueError("a cue or word ends after the leased audio")


def _audio(job: Job, root: Path, stdout: BinaryIO) -> None:
    audio = Path(job.audio_path)
    if not audio.is_absolute() or audio.parent != root or not re.fullmatch(r"[0-9a-f]{64}\.opus", audio.name):
        raise Refusal(1, "cached audio path is invalid")
    try:
        fd = os.open(audio, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Refusal(1, "cached audio is unavailable") from None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size != job.audio_bytes:
            raise Refusal(1, "cached audio does not match its receipt")
        if hashlib.file_digest(handle, "sha256").hexdigest() != job.audio_sha256:
            raise Refusal(1, "cached audio does not match its receipt")
        handle.seek(0)
        for block in iter(lambda: handle.read(1 << 20), b""):
            stdout.write(block)
        stdout.flush()


def serve(command: str, journal: Callable[[], Journal], root: Path, wake: Callable[[], None],
          stdin: BinaryIO, stdout: BinaryIO, stderr: TextIO) -> int:
    try:
        verb, args = parse(command)
        database = journal()
        match verb:
            case "request":
                response = database.request()
                if response["state"] != "empty":
                    wake()
            case "lease":
                response = database.lease()
                if response is None:
                    raise Refusal(4, "no audio is ready")
                # Let the service prepare the next slot while the Mac computes.
                _best_effort_wake(wake)
            case "audio":
                _audio(database.leased_job(args[0]), root, stdout)
                return 0
            case "renew":
                response = database.renew(args[0])
            case "submit":
                data = stdin.read(MAX_RESULT_BYTES + 1)
                if len(data) > MAX_RESULT_BYTES:
                    raise Refusal(7, "result exceeds 16 MiB")
                try:
                    response = database.accept(args[0], data, _validate)
                except LeaseRefused:
                    raise
                except ValueError as exc:
                    raise Refusal(6, str(exc)) from None
                _best_effort_wake(wake)
            case "status":
                response = database.status()
            case _:
                raise Refusal(2, "verb is outside the allowlist")
        stdout.write((json.dumps(response, ensure_ascii=True) + "\n").encode())
        stdout.flush()
        return 0
    except Refusal as exc:
        code, reason = exc.code, str(exc)
    except LeaseRefused as exc:
        code, reason = 5, str(exc)
    except Exception:
        code, reason = 1, "transcriber operation failed"
    stderr.write(f"error: {code}: {reason}\n")
    return code


def _best_effort_wake(wake: Callable[[], None]) -> None:
    try:
        wake()
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> int:
    from .cli import CACHE_DIR, JOURNAL_PATH
    from .prepare import wake

    return serve(os.environ.get("SSH_ORIGINAL_COMMAND", ""), lambda: Journal(JOURNAL_PATH), CACHE_DIR, wake,
                 sys.stdin.buffer, sys.stdout.buffer, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
