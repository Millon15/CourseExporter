"""Turns a published video on Drive into a cached 16 kHz mono Opus file, reading the MP4 by its Drive file ID.

- Resolve: exactly one object in the lesson's Drive folder with the exporter's name, size and MD5; its file ID and
  head revision are pinned from the Drive API.
- Stream: `files.get alt=media` piped into ffmpeg while the bytes are counted and hashed; a size or MD5 mismatch
  discards the audio.
- Fallback: ffmpeg cannot decode the stream (an index at the tail), so it reads the same file ID seekably through a
  loopback proxy that adds the bearer token; the pinned MD5 and revision are rechecked after the read.

The bearer token comes from the rclone remote's own OAuth client, lives in memory only and never enters argv,
a log line or an error message.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode

from .catalog import VideoAsset

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files/"
TOKEN_URL = "https://oauth2.googleapis.com/token"
METADATA_FIELDS = "id,name,size,md5Checksum,headRevisionId,parents,trashed"
CHUNK = 1 << 20
GIB = 1 << 30
MIN_FREE_BYTES = 5 * GIB
TOKEN_MARGIN_SECONDS = 120
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS = (2, 8, 30)
FFMPEG_TIMEOUT_SECONDS = 3 * 3600
DURATION_SLACK_SECONDS = 2.0
DURATION_TOLERANCE = 0.01
OPUS = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg"]

Runner = Callable[..., subprocess.CompletedProcess]
Opener = Callable[..., Any]


class SourceError(Exception):
    """The source cannot be read as pinned: missing, ambiguous, changed, or unreadable on both paths."""


class LowSpace(SourceError):
    """Less than the floor is free under the audio cache."""


class TranscodeError(Exception):
    """ffmpeg could not decode the bytes it was given."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    size: int
    md5: str
    revision: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class CachedAudio:
    path: Path
    source: DriveFile
    read_path: str
    sha256: str
    seconds: float


class RcloneToken:
    """A Drive access token refreshed through the OAuth client and refresh token of one rclone remote."""

    def __init__(self, remote: str, rclone: str = "rclone", run: Runner = subprocess.run,
                 opener: Opener = urllib.request.urlopen, clock: Callable[[], float] = time.monotonic) -> None:
        self._remote = remote.split(":", 1)[0]
        self._rclone, self._run, self._opener, self._clock = rclone, run, opener, clock
        self._token: str | None = None
        self._expires = 0.0

    def bearer(self) -> str:
        if self._token is None or self._clock() > self._expires - TOKEN_MARGIN_SECONDS:
            self._refresh()
        return self._token

    def _refresh(self) -> None:
        config = self._remote_config()
        form = {"client_id": config["client_id"], "client_secret": config.get("client_secret", ""),
                "refresh_token": json.loads(config["token"])["refresh_token"], "grant_type": "refresh_token"}
        request = urllib.request.Request(TOKEN_URL, data=urlencode(form).encode())
        try:
            with self._opener(request, timeout=60) as response:
                granted = json.load(response)
        except urllib.error.HTTPError as exc:
            raise SourceError(f"token refresh for {self._remote} failed with HTTP {exc.code}") from None
        self._token, self._expires = granted["access_token"], self._clock() + float(granted["expires_in"])

    def _remote_config(self) -> dict[str, str]:
        result = self._run([self._rclone, "config", "dump"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise SourceError(f"rclone config dump exit {result.returncode}")
        config = json.loads(result.stdout).get(self._remote) or {}
        if config.get("type") != "drive" or not config.get("client_id") or not config.get("token"):
            raise SourceError(f"rclone remote {self._remote} is not a drive remote with its own OAuth client")
        return config


class DriveApi:
    """Read-only Drive v3 calls by file ID, retried on throttling and server errors."""

    def __init__(self, token: RcloneToken, opener: Opener = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._token, self._opener, self._sleep = token, opener, sleep

    def metadata(self, file_id: str) -> DriveFile:
        query = urlencode({"fields": METADATA_FIELDS, "supportsAllDrives": "true"})
        with self._get(f"{DRIVE_FILES}{file_id}?{query}") as response:
            meta = json.load(response)
        if meta.get("trashed"):
            raise SourceError(f"Drive file {file_id} is in the trash")
        return DriveFile(id=meta["id"], name=meta["name"], size=int(meta["size"]), md5=meta["md5Checksum"].lower(),
                         revision=meta.get("headRevisionId", ""), parents=tuple(meta.get("parents", ())))

    def media(self, file_id: str, byte_range: str | None = None) -> Any:
        """An open response for the file's bytes; `byte_range` is a `Range` header value."""
        query = urlencode({"alt": "media", "supportsAllDrives": "true"})
        return self._get(f"{DRIVE_FILES}{file_id}?{query}", {"Range": byte_range} if byte_range else {})

    def _get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        for delay in (*RETRY_DELAYS, None):
            request = urllib.request.Request(url, headers={**(headers or {}),
                                                           "Authorization": f"Bearer {self._token.bearer()}"})
            try:
                return self._opener(request, timeout=120)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_STATUSES or delay is None:
                    raise SourceError(f"Drive GET {url.split('?')[0]} failed with HTTP {exc.code}") from None
            self._sleep(delay)
        raise AssertionError("unreachable")


def resolve(asset: VideoAsset, remote: str, api: DriveApi, rclone: str = "rclone",
            run: Runner = subprocess.run) -> DriveFile:
    """Pins the one Drive object in the lesson folder that has the exporter's name, size and MD5."""
    folder = asset.drive_folder(remote)
    result = run([rclone, "lsjson", "--hash", "--no-mimetype", "--files-only", folder],
                 capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise SourceError(f"rclone lsjson {folder} exit {result.returncode}")
    matches = [entry["ID"] for entry in json.loads(result.stdout) if _same_object(entry, asset)]
    if len(matches) != 1:
        raise SourceError(f"{len(matches)} Drive objects match {folder}/{asset.name}; expected exactly one")
    pinned = api.metadata(matches[0])
    if (pinned.name, pinned.size, pinned.md5) != (asset.name, asset.bytes, asset.md5):
        raise SourceError(f"Drive file {pinned.id} changed between listing and pinning")
    return pinned


class AudioCache:
    """`<root>/<md5>.opus` with a `<md5>.json` receipt; a cached entry is reused only when its hash still matches."""

    def __init__(self, root: Path, api: DriveApi, ffmpeg: str = "ffmpeg", min_free: int = MIN_FREE_BYTES) -> None:
        self._root, self._api, self._ffmpeg, self._min_free = root, api, ffmpeg, min_free

    def audio(self, source: DriveFile, expected_seconds: float) -> CachedAudio:
        """`expected_seconds` is the exporter's probed duration; a decode that falls short of it is a failed one."""
        cached = self._cached(source)
        if cached:
            return cached
        self._prepare_root()
        part = self._root / f".{source.md5}.opus.part"
        read_path = self._transcode(source, part, expected_seconds)
        return self._commit(source, part, read_path)

    def _transcode(self, source: DriveFile, part: Path, expected_seconds: float) -> str:
        try:
            self._transcode_stream(source, part)
            self._require_full_length(part, expected_seconds)
            return "stream"
        except TranscodeError:
            self._transcode_seekable(source, part)
            self._require_full_length(part, expected_seconds)
            return "range"

    def _require_full_length(self, part: Path, expected_seconds: float) -> None:
        seconds = _duration(self._ffmpeg, part)
        if abs(seconds - expected_seconds) > max(DURATION_SLACK_SECONDS, expected_seconds * DURATION_TOLERANCE):
            part.unlink(missing_ok=True)
            raise TranscodeError(f"decoded {seconds:.1f} s of a {expected_seconds:.1f} s video")

    def _transcode_stream(self, source: DriveFile, part: Path) -> None:
        digest, size = hashlib.md5(), 0
        with self._api.media(source.id) as response, _ffmpeg(self._ffmpeg, ["-i", "pipe:0"], part) as process:
            try:
                for chunk in iter(lambda: response.read(CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    process.stdin.write(chunk)
                process.stdin.close()
            except BrokenPipeError:
                raise TranscodeError("ffmpeg stopped reading the stream") from None
        if (size, digest.hexdigest()) != (source.size, source.md5):
            part.unlink(missing_ok=True)
            raise SourceError(f"Drive file {source.id} streamed {size} bytes that differ from the pinned MD5")

    def _transcode_seekable(self, source: DriveFile, part: Path) -> None:
        with _loopback_proxy(self._api, source.id) as url:
            with _ffmpeg(self._ffmpeg, ["-nostdin", "-protocol_whitelist", "http,tcp", "-i", url], part):
                pass
        reread = self._api.metadata(source.id)
        if (reread.md5, reread.revision) != (source.md5, source.revision):
            part.unlink(missing_ok=True)
            raise SourceError(f"Drive file {source.id} changed during the seekable read")

    def _commit(self, source: DriveFile, part: Path, read_path: str) -> CachedAudio:
        audio = self._root / f"{source.md5}.opus"
        part.replace(audio)
        cached = CachedAudio(path=audio, source=source, read_path=read_path, sha256=_sha256(audio),
                             seconds=_duration(self._ffmpeg, audio))
        receipt = {**asdict(cached), "path": audio.name, "source": asdict(source)}
        _write_private(self._root / f"{source.md5}.json", json.dumps(receipt, indent=1) + "\n")
        return cached

    def _cached(self, source: DriveFile) -> CachedAudio | None:
        audio, receipt = self._root / f"{source.md5}.opus", self._root / f"{source.md5}.json"
        if not (audio.is_file() and receipt.is_file()):
            return None
        saved = json.loads(receipt.read_text())
        if saved["source"]["id"] != source.id or saved["sha256"] != _sha256(audio):
            return None
        return CachedAudio(path=audio, source=source, read_path=saved["read_path"], sha256=saved["sha256"],
                           seconds=saved["seconds"])

    def _prepare_root(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root.chmod(0o700)
        free = shutil.disk_usage(self._root).free
        if free < self._min_free:
            raise LowSpace(f"{free // GIB} GiB free under {self._root}; the floor is {self._min_free // GIB} GiB")


def cut(audio: Path, start: float, seconds: float, out: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Re-encodes `seconds` of cached audio from `start`, so the clip starts exactly where its offset says."""
    with _ffmpeg(ffmpeg, ["-nostdin", "-ss", f"{start:.3f}", "-i", str(audio), "-t", f"{seconds:.3f}"], out):
        pass
    return out


@contextmanager
def _ffmpeg(ffmpeg: str, inputs: list[str], out: Path) -> Iterator[subprocess.Popen]:
    """Runs one ffmpeg into `out` as 16 kHz mono Opus; leaving the block waits for it and raises on failure."""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-y", *inputs, *OPUS,
                                    str(out)],
                                   stdin=subprocess.PIPE if "pipe:0" in inputs else subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=errors)
        try:
            yield process
        finally:
            if process.stdin and not process.stdin.closed:
                _close_quietly(process.stdin)
            code = process.wait(timeout=FFMPEG_TIMEOUT_SECONDS)
        if code != 0:
            out.unlink(missing_ok=True)
            errors.seek(0)
            raise TranscodeError(f"ffmpeg exit {code}: {errors.read(400).decode(errors='replace').strip()}")


@contextmanager
def _loopback_proxy(api: DriveApi, file_id: str) -> Iterator[str]:
    """Serves one Drive file on 127.0.0.1 with Range support, at a random path other local users cannot guess."""
    path = f"/{secrets.token_urlsafe(32)}/source.mp4"
    handler = type("SourceProxy", (_ProxyHandler,), {"api": api, "file_id": file_id, "path_token": path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}{path}"
    finally:
        server.shutdown()
        server.server_close()


class _ProxyHandler(BaseHTTPRequestHandler):
    api: DriveApi
    file_id: str
    path_token: str
    PASSED = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type")

    def do_GET(self) -> None:
        if not secrets.compare_digest(self.path, self.path_token):
            self.send_error(404)
            return
        try:
            response = self.api.media(self.file_id, self.headers.get("Range"))
        except SourceError:
            self.send_error(502)
            return
        with response:
            self.send_response(response.status)
            for name in self.PASSED:
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.end_headers()
            try:
                shutil.copyfileobj(response, self.wfile, CHUNK)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, *args: Any) -> None:
        pass


def _same_object(entry: dict[str, Any], asset: VideoAsset) -> bool:
    md5 = (entry.get("Hashes") or {}).get("md5", "").lower()
    return entry.get("Name") == asset.name and entry.get("Size") == asset.bytes and md5 == asset.md5


def _duration(ffmpeg: str, audio: Path) -> float:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe")) if os.sep in ffmpeg else "ffprobe"
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                             str(audio)], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise TranscodeError(f"ffprobe exit {result.returncode} on {audio.name}")
    return float(result.stdout.strip())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _close_quietly(stream: Any) -> None:
    try:
        stream.close()
    except BrokenPipeError:
        pass
