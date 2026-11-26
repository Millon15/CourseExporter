"""Downloads one resolved asset and proves the file.

Course pages choose media URLs, so every byte the VPS fetches goes through `PublicProxy`: a CONNECT-only proxy on
127.0.0.1 that resolves each host itself and connects only to port 443 on public addresses, which covers redirects,
playlist variants, segments and key URLs alike. A signed URL reaches yt-dlp through a 0600 info file or stdin, never
argv, and every error message passes through `redact` before it is stored or printed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import select
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Generator
from urllib.parse import urlsplit

from .model import Asset, Media, UnsupportedMedia
from .state import VerifiedFile

URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
VIDEO_TIMEOUT_SECONDS = 6 * 3600
FILE_TIMEOUT_SECONDS = 600
CHUNK = 1 << 20
LOCAL_ONLY = ["-protocol_whitelist", "file", "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2"]
"""ffprobe and ffmpeg may read only the local file, and only as MP4: a playlist posing as a video fetches nothing."""
CHECK_EVERY = 8 << 20
MANIFEST_BYTES = 1 << 18
TEXT_BYTES = 4 << 20
KEY_LINE = re.compile(r"^#EXT-X-(?:SESSION-)?KEY:(.*)$", re.MULTILINE)
KEY_METHOD = re.compile(r"(?:^|,)METHOD=([A-Z0-9-]+)")
DURATION_TOLERANCE = 0.02
DURATION_SLACK_SECONDS = 5.0
WATCH_SECONDS = 5.0
NATIVE_PROTOCOLS = frozenset({"https", "m3u8_native", "http_dash_segments"})
"""Protocols yt-dlp's own downloader fetches through --proxy; anything else (ffmpeg, rtmp, ...) could bypass it."""

Runner = Callable[..., subprocess.CompletedProcess]
Opener = Callable[..., Any]
AbortCheck = Callable[[], "str | None"]


class DownloadError(Exception):
    """A download failed; the message is already redacted."""


class VerificationError(Exception):
    """A downloaded file failed its checks; the message is already redacted."""


class Aborted(Exception):
    """The staging budget stopped a writer (floor, cap or lesson reservation); the run must stop, not move on."""


@dataclass(frozen=True)
class Tools:
    ytdlp: str = "yt-dlp"
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"


def redact(text: str, limit: int = 600) -> str:
    return URL_PATTERN.sub("<url>", text).strip()[-limit:]


def public_addresses(host: str) -> list[str]:
    """Every address of `host`, or an empty list when any of them is not public (loopback, private, link-local)."""
    try:
        addresses = sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError:
        return []
    if not addresses or any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        return []
    return addresses


class PublicProxy:
    """CONNECT-only proxy on 127.0.0.1 for the downloader: port 443 on public addresses, nothing else."""

    def __init__(self, resolve: Callable[[str], list[str]] = public_addresses) -> None:
        self.refused: list[str] = []
        resolve_host, refused = resolve, self.refused

        class Tunnel(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                head = _read_head(self.request)
                match = re.match(rb"CONNECT ([A-Za-z0-9.-]+):443 HTTP/1\.[01]\r\n", head)
                addresses = resolve_host(match.group(1).decode()) if match else []
                if not addresses:
                    refused.append(match.group(1).decode() if match else "non-CONNECT request")
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                try:
                    with socket.create_connection((addresses[0], 443), timeout=30) as upstream:
                        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        _relay(self.request, upstream)
                except OSError:
                    return  # a reset tunnel is the client's to retry; the proxy only ends it quietly

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Tunnel)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "PublicProxy":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _read_head(sock: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        head += chunk
    return head


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    while True:
        readable, _, failed = select.select(sockets, [], sockets, 300)
        if failed or not readable:
            return
        for source in readable:
            data = source.recv(CHUNK)
            if not data:
                return
            (upstream if source is client else client).sendall(data)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise UnsupportedMedia("media redirected to a non-https URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _MandatoryProxy(urllib.request.ProxyHandler):
    """ProxyHandler that never consults no_proxy: every request goes through the public-only proxy."""

    def proxy_open(self, req, proxy, type):
        orig_type = req.type
        parts = urlsplit(proxy)
        req.set_proxy(parts.netloc, parts.scheme)
        if orig_type in (parts.scheme, "https"):
            return None
        return self.parent.open(req, timeout=req.timeout)


def downloader_env() -> dict[str, str]:
    """The environment for yt-dlp: no inherited proxy or no_proxy variable can redirect or bypass --proxy."""
    names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"}
    return {k: v for k, v in os.environ.items() if k.lower() not in names}


def proxied_opener(proxy_url: str) -> Opener:
    """An opener with no file, ftp or data handler: https through the proxy, https-only redirects."""
    opener = urllib.request.OpenerDirector()
    for handler in (_MandatoryProxy({"https": proxy_url, "http": proxy_url}),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(), _HttpsOnlyRedirects(),
                    urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler()):
        opener.add_handler(handler)
    return opener.open


def fetch_public_text(url: str, headers: dict[str, str] | None = None, opener: Opener | None = None) -> str:
    """GETs a page on the VPS through the public-only proxy; providers use it for signed player pages so the signed
    URL never travels to the Mac in argv."""
    if urlsplit(url).scheme != "https":
        raise UnsupportedMedia("media URL is not https")
    with _opener_scope(opener) as open_url:
        try:
            with open_url(urllib.request.Request(url, headers=headers or {}), timeout=60) as response:
                return response.read(TEXT_BYTES).decode("utf-8", errors="replace")
        except OSError as error:
            raise DownloadError(redact(f"page fetch: {type(error).__name__}: {error}")) from None


@contextmanager
def _opener_scope(opener: Opener | None) -> Generator[Opener]:
    if opener is not None:
        yield opener
        return
    with PublicProxy() as proxy:
        yield proxied_opener(proxy.url)


def download(media: Media, asset: Asset, stem: Path, max_height: int, tools: Tools = Tools(),
             run: Runner = subprocess.run, opener: Opener | None = None, proxy_url: str | None = None,
             abort: AbortCheck = lambda: None, popen: Callable[..., Any] = subprocess.Popen) -> Path:
    """Fetches the asset to `stem` plus its extension; a finished file from an earlier run is reused, not refetched.

    Raises UnsupportedMedia before any media byte is written when the URL is not https or a selected HLS playlist
    declares encryption; `abort` is polled during a video transfer and ends it with DownloadError."""
    if urlsplit(media.url).scheme != "https":
        raise UnsupportedMedia("media URL is not https")
    stem.parent.mkdir(parents=True, exist_ok=True)
    target = stem.with_name(stem.name + (_file_suffix(asset, media) if media.kind == "file" else ".mp4"))
    if target.is_file() and target.stat().st_size > 0:
        return target
    fetch = _Fetch(media, target, max_height, tools, run, abort, popen)
    if opener is not None and proxy_url is not None:
        return fetch.through(opener, proxy_url)
    with PublicProxy() as proxy:
        return fetch.through(proxied_opener(proxy.url), proxy.url)


@dataclass(frozen=True)
class _Fetch:
    media: Media
    target: Path
    max_height: int
    tools: Tools
    run: Runner
    abort: AbortCheck
    popen: Callable[..., Any]

    def through(self, opener: Opener, proxy_url: str) -> Path:
        if self.media.kind == "file":
            return _download_file(self.media, self.target, opener, self.abort)
        info = _select_formats(self.media, self.max_height, self.tools, self.run, proxy_url)
        refuse_encrypted_formats(info, self.media.headers, opener)
        return _download_selected(info, self.target, self.tools, proxy_url, self.abort, self.popen)


def _select_formats(media: Media, max_height: int, tools: Tools, run: Runner, proxy_url: str) -> dict:
    """Asks yt-dlp which formats it would download, without downloading; the height cap has no uncapped fallback."""
    selector = f"bv*[height<=?{max_height}]+ba/b[height<=?{max_height}]"
    args = [tools.ytdlp, "--batch-file", "-", "-J", "--no-playlist", "--proxy", proxy_url, "-f", selector]
    for name, value in media.headers.items():
        args += ["--add-header", f"{name}:{value}"]
    result = run(args, input=media.url + "\n", capture_output=True, text=True, timeout=300, env=downloader_env())
    if result.returncode != 0:
        raise DownloadError(f"yt-dlp format selection exit {result.returncode}: {redact(result.stderr)}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DownloadError("yt-dlp format selection printed no JSON") from None


def refuse_encrypted_formats(info: dict, headers: dict[str, str], opener: Opener) -> None:
    """Every format yt-dlp selected (video and audio) must be DRM-free; an HLS one must declare no key."""
    for fmt in info.get("requested_formats") or [info]:
        if fmt.get("has_drm"):
            raise UnsupportedMedia("yt-dlp reports DRM on a selected format")
        url = fmt.get("url") or ""
        if fmt.get("protocol") not in NATIVE_PROTOCOLS or urlsplit(url).scheme != "https":
            raise UnsupportedMedia(f"selected format uses protocol {fmt.get('protocol')!r}, outside the proxied set")
        if not str(fmt.get("protocol", "")).startswith("m3u8"):
            continue
        playlist = _read_manifest(url, fmt.get("http_headers") or headers, opener)
        if playlist is None:
            continue
        _refuse_keys(playlist)
        if "#EXT-X-STREAM-INF" in playlist:
            raise UnsupportedMedia("selected HLS format is a master playlist, not a media playlist")


def _read_manifest(url: str, headers: dict[str, str], opener: Opener) -> str | None:
    """The playlist text, or None when the body is not an HLS playlist (a plain MP4)."""
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=60) as response:
            head = response.read(MANIFEST_BYTES)
    except OSError as error:
        raise DownloadError(redact(f"playlist check: {type(error).__name__}: {error}")) from None
    text = head.decode("utf-8", errors="replace").lstrip("﻿")
    return text if text.startswith("#EXTM3U") else None


def _refuse_keys(playlist: str) -> None:
    for attributes in KEY_LINE.findall(playlist):
        method = KEY_METHOD.search(attributes)
        if method is None or method.group(1) != "NONE":
            raise UnsupportedMedia(f"HLS playlist declares encryption (METHOD={method.group(1) if method else '?'})")


def _download_selected(info: dict, target: Path, tools: Tools, proxy_url: str, abort: AbortCheck,
                       popen: Callable[..., Any]) -> Path:
    """Downloads exactly the formats checked above: yt-dlp reloads that info from a 0600 file in the lesson dir."""
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".info-", suffix=".json", delete=True) as handle:
        json.dump(info, handle)
        handle.flush()
        args = [tools.ytdlp, "--load-info-json", handle.name, "--proxy", proxy_url, "--downloader", "native",
                "--no-progress", "--no-mtime", "--retries", "10", "--fragment-retries", "20",
                "--merge-output-format", "mp4", "-o", str(target)]
        code, stderr = _run_watched(args, abort, popen)
    if code != 0 or not target.is_file():
        raise DownloadError(f"yt-dlp exit {code}: {redact(stderr)}")
    return target


def _run_watched(args: list[str], abort: AbortCheck, popen: Callable[..., Any]) -> tuple[int, str]:
    """Runs yt-dlp in its own session so an abort, a timeout or an interrupt stops it and its ffmpeg children."""
    with tempfile.TemporaryFile("w+") as errors:
        process = popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, text=True,
                        env=downloader_env(), start_new_session=True)
        deadline = time.monotonic() + VIDEO_TIMEOUT_SECONDS
        try:
            while process.poll() is None:
                if reason := abort():
                    raise Aborted(reason)
                if time.monotonic() > deadline:
                    raise DownloadError("yt-dlp timed out")
                time.sleep(WATCH_SECONDS)
        except BaseException:
            _stop_group(process)
            raise
        errors.seek(0)
        return process.returncode, errors.read()


def _stop_group(process: Any, grace_seconds: float = 30) -> None:
    """SIGTERM to the whole group, then SIGKILL to whatever is still in it after the grace, parent or not: an ffmpeg
    child that outlives yt-dlp must not keep writing after the reservation is released."""
    _signal_group(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        if not _group_alive(process.pid):
            break
        time.sleep(0.2)
    if _group_alive(process.pid):
        _signal_group(process.pid, signal.SIGKILL)
    process.wait()


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _download_file(media: Media, target: Path, opener: Opener, abort: AbortCheck) -> Path:
    """Streams an attachment to .part, asking `abort` every CHECK_EVERY bytes; a stopped download leaves no target."""
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(media.url, headers=media.headers)
    written = 0
    try:
        with opener(request, timeout=FILE_TIMEOUT_SECONDS) as response, partial.open("wb") as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
                written += len(chunk)
                if written % CHECK_EVERY < len(chunk) and (reason := abort()):
                    raise Aborted(reason)
    except OSError as error:
        raise DownloadError(redact(f"{type(error).__name__}: {error}")) from None
    if reason := abort():
        raise Aborted(reason)
    os.replace(partial, target)
    return target


def _file_suffix(asset: Asset, media: Media) -> str:
    for name in (asset.title, PurePosixPath(urlsplit(media.url).path).name):
        suffix = PurePosixPath(name).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            return suffix
    return ".bin"


def verify(path: Path, kind: str, tools: Tools = Tools(), decode: bool = False, expected_seconds: float | None = None,
           run: Runner = subprocess.run) -> VerifiedFile:
    """Proves a file is whole: nonempty and hashed; a video also needs video and audio streams, a positive duration
    that matches the provider's when it gave one, and with `decode` a clean full decode."""
    if not path.is_file() or path.stat().st_size == 0:
        raise VerificationError(f"{path.name}: missing or empty")
    duration = _probe_video(path, tools, run, expected_seconds) if kind == "video" else 0.0
    if kind == "video" and decode:
        _decode_video(path, tools, run)
    sha256, md5 = _digests(path)
    return VerifiedFile(path=path, bytes=path.stat().st_size, sha256=sha256, duration=duration, md5=md5)


def set_aside(path: Path) -> Path:
    """Renames a rejected file so the next run downloads the asset again; the bytes stay for inspection."""
    rejected = path.with_name(path.name + ".rejected")
    os.replace(path, rejected)
    return rejected


def _probe_video(path: Path, tools: Tools, run: Runner, expected_seconds: float | None) -> float:
    result = run([tools.ffprobe, "-v", "error", *LOCAL_ONLY, "-print_format", "json", "-show_streams", "-show_format",
                  str(path)], capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise VerificationError(f"{path.name}: ffprobe exit {result.returncode}: {redact(result.stderr)}")
    probe = json.loads(result.stdout or "{}")
    kinds = {stream.get("codec_type") for stream in probe.get("streams", [])}
    for needed in ("video", "audio"):
        if needed not in kinds:
            raise VerificationError(f"{path.name}: no {needed} stream")
    duration = float(probe.get("format", {}).get("duration") or 0.0)
    if duration <= 0:
        raise VerificationError(f"{path.name}: no duration")
    if expected_seconds and abs(duration - expected_seconds) > max(DURATION_SLACK_SECONDS,
                                                                   expected_seconds * DURATION_TOLERANCE):
        raise VerificationError(f"{path.name}: {duration:.0f}s, provider says {expected_seconds:.0f}s")
    return duration


def _decode_video(path: Path, tools: Tools, run: Runner) -> None:
    result = run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", *LOCAL_ONLY, "-i", str(path), "-f", "null", "-"],
                 capture_output=True, text=True, timeout=VIDEO_TIMEOUT_SECONDS)
    if result.returncode != 0 or result.stderr.strip():
        raise VerificationError(f"{path.name}: decode failed: {redact(result.stderr)}")


def _digests(path: Path) -> tuple[str, str]:
    """SHA-256 for the record, MD5 because Drive reports it and publication compares against it."""
    sha256, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()
