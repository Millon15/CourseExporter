"""Mac worker: takes one prepared audio file at a time from the VPS over ssh, runs D2 on it and submits the result.

Loop: `request` until the VPS answers ready (or exit on deferred or empty), `lease`, fetch the Opus into scratch and
check its SHA-256, run D2 while a thread renews the lease every 120 s, `submit`, check the receipt, clear scratch.

- The Mac never holds a video, a Drive credential or a destination id; it cannot publish.
- Scratch is one 0700 directory holding at most one item under fixed names; the worker only overwrites or unlinks
  those names. A result whose submit reply was lost stays there and is resubmitted, identical, on the next start.
- One instance at a time (flock on the scratch directory).

Run: `uv run --project tools/transcribe-d2 python -m course_exporter.transcribe.mac_worker [--once]`
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from . import d2_pins as pins
from . import segments

HOST = "jarvis-transcribe"
SCRATCH = Path.home() / "Library" / "Caches" / "course-transcribe"
RENEW_EVERY_S = 120
PREPARING_POLL_S = 30
MAX_PREPARING_WAIT_S = 1800
SUBMIT_ATTEMPTS = 3
SUBMIT_RETRY_S = 10
CHUNK = 1 << 20
SSH_TRANSPORT_ERROR = 255
EXIT_NOTHING_READY = 4
EXIT_LEASE_FENCED = 5
EXIT_RESULT_REFUSED = 6
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MODELS_MISSING = 3
EXIT_BUSY = 75
LEASE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHA256 = re.compile(r"[0-9a-f]{64}")

Transcriber = Callable[[Path, str, str], dict[str, Any]]


class RemoteError(Exception):
    def __init__(self, verb: str, code: int, detail: str) -> None:
        super().__init__(f"{verb} exited {code}: {detail}")
        self.verb = verb
        self.code = code


class LeaseLost(Exception):
    """The VPS fenced the lease during compute; the result is not submitted."""


@dataclass(frozen=True)
class Lease:
    lease_id: str
    job_id: str
    expires_at: str
    audio_sha256: str
    audio_bytes: int
    duration_s: float
    language: str
    producer_config: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Lease:
        lease = cls(**{key: payload[key] for key in cls.__dataclass_fields__})
        _require(LEASE_ID.fullmatch(str(lease.lease_id)) is not None, "lease_id is not a safe token")
        _require(SHA256.fullmatch(str(lease.audio_sha256)) is not None, "audio_sha256 is not hex")
        _require(isinstance(lease.audio_bytes, int) and lease.audio_bytes > 0, "audio_bytes is not positive")
        _require(lease.language in segments.LANGUAGES, "language is not ru or uk")
        _require(lease.producer_config == pins.PRODUCER_CONFIG, "the VPS asks for another D2 config")
        return lease


class Remote:
    """The six VPS verbs over the restricted ssh alias; nothing else reaches the VPS."""

    def __init__(self, host: str = HOST, run: Callable[..., Any] = subprocess.run,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self._host = host
        self._run = run
        self._popen = popen

    def request(self) -> dict[str, Any]:
        return json.loads(self._call("request"))

    def lease(self) -> Lease | None:
        try:
            return Lease.parse(json.loads(self._call("lease")))
        except RemoteError as error:
            if error.code == EXIT_NOTHING_READY:
                return None
            raise

    def renew(self, lease: Lease) -> None:
        self._call("renew", lease.lease_id)

    def submit(self, lease_id: str, data: bytes) -> dict[str, Any]:
        return json.loads(self._call("submit", lease_id, data))

    def audio(self, lease: Lease, target: Path) -> str:
        """Streams the Opus into `target` and returns its SHA-256."""
        digest = hashlib.sha256()
        with target.open("wb") as out, self._popen(self._argv("audio", lease.lease_id), stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL) as process:
            for chunk in iter(lambda: process.stdout.read(CHUNK), b""):
                digest.update(chunk)
                out.write(chunk)
            detail = process.stderr.read().decode(errors="replace").strip()
        if process.returncode:
            raise RemoteError("audio", process.returncode, detail)
        return digest.hexdigest()

    def _call(self, verb: str, argument: str | None = None, data: bytes | None = None) -> bytes:
        result = self._run(self._argv(verb, argument), input=data or b"", capture_output=True, check=False)
        if result.returncode:
            raise RemoteError(verb, result.returncode, result.stderr.decode(errors="replace").strip())
        return result.stdout

    def _argv(self, verb: str, argument: str | None) -> list[str]:
        options = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
        return ["ssh", *options, self._host, verb, *([argument] if argument else [])]


class Scratch:
    """The one-item scratch directory: fixed names, 0700, owned by this user."""

    def __init__(self, root: Path = SCRATCH) -> None:
        self.root = root
        self.audio = root / "item.opus"
        self.partial = root / "item.opus.part"
        self.result = root / "item.result.json"
        self.lease = root / "item.lease.json"

    def open(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), f"{self.root} is not my directory")
        _require(info.st_mode & 0o077 == 0, f"{self.root} is open to other users")

    @contextmanager
    def locked(self) -> Generator[bool]:
        with (self.root / ".lock").open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def holds_audio(self, sha256: str) -> bool:
        return self.audio.is_file() and _file_sha256(self.audio) == sha256

    def write(self, path: Path, data: bytes) -> None:
        partial = path.with_name(path.name + ".tmp")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        partial.replace(path)

    def clear(self) -> None:
        for path in (self.audio, self.partial, self.result, self.lease):
            path.unlink(missing_ok=True)


class Worker:
    def __init__(self, remote: Remote, scratch: Scratch, transcriber: Transcriber,
                 log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 renew_every: float = RENEW_EVERY_S) -> None:
        self._remote = remote
        self._scratch = scratch
        self._transcribe = transcriber
        self._log = log
        self._sleep = sleep
        self._renew_every = renew_every

    def run(self, once: bool = False) -> int:
        self._resubmit_pending()
        while (lease := self._next_lease()) is not None:
            self._process(lease)
            if once:
                break
        return EXIT_OK

    def _resubmit_pending(self) -> None:
        if not (self._scratch.result.is_file() and self._scratch.lease.is_file()):
            return
        lease = Lease.parse(json.loads(self._scratch.lease.read_bytes()))
        self._log(f"resubmitting the kept result of job {lease.job_id}")
        try:
            self._submit(lease, self._scratch.result.read_bytes())
        except RemoteError as error:
            if error.code not in (EXIT_LEASE_FENCED, EXIT_RESULT_REFUSED):
                raise
            self._log(f"the VPS refused the kept result ({error}); dropping it")
            self._scratch.clear()

    def _next_lease(self) -> Lease | None:
        waited = 0.0
        while waited <= MAX_PREPARING_WAIT_S:
            answer = self._remote.request()
            state = answer.get("state")
            if state == "ready" and (lease := self._remote.lease()) is not None:
                return lease
            if state not in ("ready", "preparing"):
                self._log(f"nothing to do: {state} ({answer.get('reason')})")
                return None
            pause = answer.get("retry_after_s") or PREPARING_POLL_S
            self._sleep(pause)
            waited += pause
        self._log(f"the VPS stayed preparing for {MAX_PREPARING_WAIT_S} s; stopping")
        return None

    def _process(self, lease: Lease) -> None:
        self._log(f"job {lease.job_id}: {lease.duration_s:.0f} s of {lease.language} audio")
        self._scratch.write(self._scratch.lease, json.dumps(asdict(lease)).encode())
        self._fetch_audio(lease)
        data = self._compute(lease)
        self._scratch.write(self._scratch.result, data)
        self._submit(lease, data)

    def _fetch_audio(self, lease: Lease) -> None:
        if self._scratch.holds_audio(lease.audio_sha256):
            self._log("reusing the scratch audio; its SHA-256 matches")
            return
        self._scratch.audio.unlink(missing_ok=True)
        sha = self._remote.audio(lease, self._scratch.partial)
        size = self._scratch.partial.stat().st_size
        if sha != lease.audio_sha256 or size != lease.audio_bytes:
            self._scratch.partial.unlink()
            raise RemoteError("audio", EXIT_FAILED, f"got {size} bytes sha256 {sha}, the lease names another file")
        self._scratch.partial.replace(self._scratch.audio)

    def _compute(self, lease: Lease) -> bytes:
        with self._renewing(lease) as lost:
            doc = self._transcribe(self._scratch.audio, lease.language, lease.job_id)
        if lost.is_set():
            raise LeaseLost(f"job {lease.job_id}: the VPS fenced lease {lease.lease_id} during compute")
        _require(doc["source"]["audio_sha256"] == lease.audio_sha256, "D2 read another audio file")
        segments.validate(doc)
        return json.dumps(doc, ensure_ascii=False, allow_nan=False).encode()

    @contextmanager
    def _renewing(self, lease: Lease) -> Generator[threading.Event]:
        stop, lost = threading.Event(), threading.Event()
        thread = threading.Thread(target=self._renew_until, args=(lease, stop, lost), daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stop.set()
            thread.join()

    def _renew_until(self, lease: Lease, stop: threading.Event, lost: threading.Event) -> None:
        while not stop.wait(self._renew_every):
            try:
                self._remote.renew(lease)
            except RemoteError as error:
                self._log(f"renew failed: {error}")
                if error.code == EXIT_LEASE_FENCED:
                    lost.set()
                    return

    def _submit(self, lease: Lease, data: bytes) -> None:
        receipt = self._submit_with_retries(lease.lease_id, data)
        expected = hashlib.sha256(data).hexdigest()
        _require(receipt.get("job_id") == lease.job_id and receipt.get("result_sha256") == expected,
                 f"the receipt names another result: {receipt}")
        self._log(f"job {lease.job_id}: received, receipt {receipt.get('receipt_id')}")
        self._scratch.clear()

    def _submit_with_retries(self, lease_id: str, data: bytes) -> dict[str, Any]:
        for attempt in range(1, SUBMIT_ATTEMPTS + 1):
            try:
                return self._remote.submit(lease_id, data)
            except RemoteError as error:
                if error.code != SSH_TRANSPORT_ERROR or attempt == SUBMIT_ATTEMPTS:
                    raise
                self._log(f"submit lost its connection, resubmitting the identical result ({attempt})")
                self._sleep(SUBMIT_RETRY_S)
        raise AssertionError("unreachable")


class LazyD2:
    """Loads the D2 models on the first item only, so an empty queue costs no model load."""

    def __init__(self) -> None:
        self._models = None

    def __call__(self, audio: Path, language: str, job_id: str) -> dict[str, Any]:
        from . import d2

        if self._models is None:
            self._models = d2.load_models()
        return d2.transcribe(self._models, audio, language, job_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe prepared course audio from the VPS with D2")
    parser.add_argument("--once", action="store_true", help="stop after one item")
    parser.add_argument("--host", default=HOST, help="ssh alias of the restricted VPS key")
    parser.add_argument("--scratch", type=Path, default=SCRATCH)
    args = parser.parse_args(argv)
    scratch = Scratch(args.scratch)
    scratch.open()
    with scratch.locked() as acquired:
        if not acquired:
            print(f"another worker holds {scratch.root / '.lock'}", file=sys.stderr)
            return EXIT_BUSY
        return _run(Worker(Remote(args.host), scratch, LazyD2(), log=_log), args.once)


def _run(worker: Worker, once: bool) -> int:
    from .d2 import ModelUnavailable

    try:
        return worker.run(once)
    except ModelUnavailable as error:
        print(error, file=sys.stderr)
        return EXIT_MODELS_MISSING
    except (RemoteError, LeaseLost, ValueError) as error:
        print(f"mac worker stopped: {error}", file=sys.stderr)
        return EXIT_FAILED


def _log(line: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}", flush=True)


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
