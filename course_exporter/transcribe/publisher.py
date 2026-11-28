"""Create and reconcile one immutable UCH transcript by reserved Drive IDs."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import math
import re
import secrets
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlencode

from .journal import Job, Journal, PlannedFile
from .source import DRIVE_FILES, DriveApi, RcloneToken, SourceChanged, _file_id, recheck

CREATE_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
ROLES = ("vtt", "text", "segments", "manifest", "selection")
SUFFIXES = {"vtt": "vtt", "text": "txt", "segments": "segments.json", "manifest": "manifest.json"}
MAX_GENERATION = 2147483647
META_LIMIT = 128 * 1024
DATA_LIMIT = 8 * 1024 * 1024
OBJECT_FIELDS = "id,name,size,parents,trashed,mimeType"
RETRYABLE = {408, 429, 500, 502, 503, 504}


class PublishError(Exception):
    pass


class Retryable(PublishError):
    pass


class Conflict(PublishError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Writer:
    """Restricted use of a dedicated rclone OAuth client through Drive's create API."""

    def __init__(self, token: RcloneToken, opener=None) -> None:
        self._token = token
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    def _request(self, url: str, limit: int, data: bytes | None = None,
                 content_type: str | None = None) -> bytes | None:
        headers = {"Authorization": f"Bearer {self._token.bearer()}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self._open(req, timeout=60) as response:
                body = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404 and data is None:
                return None
            if exc.code == 409 and data is not None:
                raise Conflict("Drive create returned HTTP 409") from None
            if exc.code in RETRYABLE:
                raise Retryable(f"Drive returned HTTP {exc.code}") from None
            raise PublishError(f"Drive returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            raise Retryable("Drive request disconnected or timed out") from None
        if len(body) > limit:
            raise PublishError("Drive response exceeds its byte limit")
        return body

    def metadata(self, file_id: str) -> dict | None:
        query = urlencode({"fields": OBJECT_FIELDS, "supportsAllDrives": "true"})
        body = self._request(f"{DRIVE_FILES}{_file_id(file_id)}?{query}", META_LIMIT)
        return _object(body)

    def content(self, file_id: str, limit: int) -> bytes | None:
        query = urlencode({"alt": "media", "supportsAllDrives": "true"})
        return self._request(f"{DRIVE_FILES}{_file_id(file_id)}?{query}", limit)

    def children(self, parent: str) -> list[dict]:
        config = {"q": f"'{_file_id(parent)}' in parents and trashed = false", "pageSize": "1000",
                  "fields": f"nextPageToken,incompleteSearch,files({OBJECT_FIELDS})",
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        items: list[dict] = []
        tokens: set[str] = set()
        while True:
            body = self._request(f"{DRIVE_FILES.rstrip('/')}?{urlencode(config)}", DATA_LIMIT)
            page = _object(body)
            if page is None or page.get("incompleteSearch"):
                raise PublishError("Drive sibling lookup is incomplete")
            items.extend(page["files"])
            token = page.get("nextPageToken")
            if not token:
                return items
            if token in tokens:
                raise PublishError("Drive repeated a listing page")
            tokens.add(token)
            config["pageToken"] = token

    def reserve(self, count: int) -> list[str]:
        query = urlencode({"count": count, "space": "drive", "type": "files"})
        result = _object(self._request(f"{DRIVE_FILES}generateIds?{query}", META_LIMIT))
        ids = result.get("ids", []) if result else []
        if len(ids) != count or len(set(ids)) != count:
            raise PublishError("Drive did not return distinct reserved IDs")
        return [_file_id(file_id) for file_id in ids]

    def create(self, file: PlannedFile) -> None:
        boundary = "uch_" + secrets.token_hex(24)
        mime = {"vtt": "text/vtt", "text": "text/plain"}.get(file.role, "application/json")
        metadata = json.dumps({"id": file.id, "name": file.name, "parents": [file.parent], "mimeType": mime}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + metadata
                + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() + file.data
                + f"\r\n--{boundary}--\r\n".encode())
        query = urlencode({"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"})
        self._request(f"{CREATE_URL}?{query}", META_LIMIT, body, f"multipart/related; boundary={boundary}")


def _object(body: bytes | None) -> dict | None:
    if body is None:
        return None
    try:
        node = json.loads(body)
    except (UnicodeError, ValueError, RecursionError):
        return None
    return node if isinstance(node, dict) else None


def _prefix(job: Job) -> str:
    return re.sub(r"\.mp4$", "", job.source.name, flags=re.IGNORECASE) + f".{job.source.md5}."


def _generation(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix):
        return None
    match = re.fullmatch(r"selected\.g([0-9]{4,10})\.json", name[len(prefix):])
    if not match:
        return None
    number = int(match[1])
    return number if number <= MAX_GENERATION and str(number).zfill(4) == match[1] else None


def _limit(role: str) -> int:
    return META_LIMIT if role in ("manifest", "selection") else DATA_LIMIT


def _same_video(document: dict, job: Job) -> bool:
    return (type(document.get("schema_version")) is int and document["schema_version"] == 1
            and document.get("drive_file_id") == job.source.id and document.get("source_md5") == job.source.md5)


def _segments(body: bytes, duration: float | None) -> bool:
    doc = _object(body)
    if doc is None or type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
        return False
    cues = doc.get("segments")
    if not isinstance(cues, list):
        return False
    previous = 0
    for cue in cues:
        if not isinstance(cue, dict):
            return False
        start, end, speaker, text = (cue.get(key) for key in ("start", "end", "speaker", "text"))
        if any(type(value) not in (int, float) or abs(value) > 1e300 or not math.isfinite(value)
               for value in (start, end)):
            return False
        if start < previous or end <= start or (duration is not None and end > duration + 0.01):
            return False
        if not all(isinstance(value, str) and value.strip() and "\0" not in value for value in (speaker, text)):
            return False
        if len(speaker.encode("utf-16-le", errors="surrogatepass")) > 256:
            return False
        previous = start
    return True


def _contents(writer: Writer, entry: dict, limit: int) -> bytes | None:
    size = entry.get("size")
    if not isinstance(size, (str, int)) or not str(size).isdigit() or int(size) > limit:
        return None
    body = writer.content(entry["id"], limit)
    return body if body is not None and len(body) == int(size) else None


def _checked_ref(writer: Writer, reference, siblings: dict, limit: int) -> tuple[str, str, bytes] | None:
    if not isinstance(reference, dict):
        return None
    identifier, sha = reference.get("id"), reference.get("sha256")
    if not isinstance(identifier, str) or identifier not in siblings:
        return None
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        return None
    entry = siblings[identifier]
    body = _contents(writer, entry, limit)
    if body is None or hashlib.sha256(body).hexdigest() != sha:
        return None
    return entry["name"], sha, body


def _valid_selection(writer: Writer, job: Job, generation: int, selection: dict,
                     siblings: dict, duration: float | None) -> bool:
    if not _same_video(selection, job) or type(selection.get("generation")) is not int:
        return False
    if selection["generation"] != generation or not isinstance(selection.get("outputs"), dict):
        return False
    manifest = _checked_ref(writer, selection.get("manifest"), siblings, META_LIMIT)
    if manifest is None:
        return False
    name, _, data = manifest
    prefix, manifest_suffix = _prefix(job), ".manifest.json"
    if not name.startswith(prefix) or not name.endswith(manifest_suffix):
        return False
    stem = name[:-len(manifest_suffix)]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", stem[len(prefix):]):
        return False
    meta = _object(data)
    if meta is None or not _same_video(meta, job) or meta.get("language") not in ("ru", "uk"):
        return False
    for key in ("timing_kind", "speaker_method"):
        if not isinstance(meta.get(key), str) or not meta[key] or "\0" in meta[key]:
            return False
    sums = meta.get("output_sha256s")
    if not isinstance(sums, dict):
        return False
    for role in ("vtt", "text", "segments"):
        loaded = _checked_ref(writer, selection["outputs"].get(role), siblings, DATA_LIMIT)
        if loaded is None:
            return False
        name, sha, body = loaded
        if name != f"{stem}.{SUFFIXES[role]}" or sums.get(role) != sha:
            return False
        if role == "segments":
            if not _segments(body, duration):
                return False
        else:
            try:
                value = body.decode("utf-8-sig")
            except UnicodeDecodeError:
                return False
            if "\0" in value or (role == "vtt" and not re.match(r"WEBVTT(?:\s|$)", value)):
                return False
    return True


class Publisher:
    def __init__(self, journal: Journal, writer: Writer, reader: DriveApi, root_id: str,
                 still_published: Callable[[Job], bool]) -> None:
        self.journal, self.writer, self.reader = journal, writer, reader
        self.root_id, self.still_published = _file_id(root_id), still_published

    def current(self, job: Job):
        if not self.still_published(job):
            raise SourceChanged("exporter no longer lists this revision as published")
        return recheck(self.reader, job.source, "before publication")

    def _parent(self, job: Job) -> str:
        (parent,) = job.source.parents
        path = [job.asset.course, *job.asset.lesson_dir.split("/")]
        cursor = parent
        for name in reversed(path):
            node = self.writer.metadata(cursor)
            if (not node or node.get("id") != cursor or node.get("name") != name
                    or node.get("trashed") or node.get("mimeType") != FOLDER
                    or len(node.get("parents", [])) != 1):
                raise SourceChanged("pinned lesson folder is outside the expected Courses hierarchy")
            cursor = node["parents"][0]
        root = self.writer.metadata(self.root_id)
        if cursor != self.root_id or not root or root.get("id") != cursor or root.get("trashed") or (
            root.get("mimeType") != FOLDER or root.get("name") != "Courses"
        ):
            raise SourceChanged("destination is outside the pinned Courses root")
        visible = self.writer.metadata(job.source.id)
        if not self._matches(visible, job.source.id, parent, job.source.name, job.source.size):
            raise SourceChanged("publisher cannot verify the source video in the pinned folder")
        return parent

    @staticmethod
    def _matches(meta: dict | None, file_id: str, parent: str, name: str, size: int) -> bool:
        return bool(meta and meta.get("id") == file_id and not meta.get("trashed") and meta.get("parents") == [parent]
                    and meta.get("name") == name and str(meta.get("size")) == str(size))

    def already_compliant(self, job: Job) -> bool:
        current = self.current(job)
        parent = self._parent(job)
        items = self.writer.children(parent)
        siblings = {item["id"]: item for item in items}
        selections: dict[int, list[dict]] = {}
        prefix = _prefix(job)
        for entry in siblings.values():
            generation = _generation(entry["name"], prefix)
            if generation is not None:
                selections.setdefault(generation, []).append(entry)
        for generation in sorted(selections, reverse=True):
            bodies = [_contents(self.writer, entry, META_LIMIT) for entry in selections[generation]]
            first = bodies[0]
            if first is None or any(body != first for body in bodies):
                continue
            value = _object(first)
            if value is not None and _valid_selection(self.writer, job, generation, value, siblings,
                                                     current.video_seconds):
                return True
        return False

    def publish(self, job: Job, data: bytes) -> None:
        lock_path = self.journal.path.with_name("publisher.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.current(job)
            parent = self._parent(job)
            generation, files = self.journal.plan(job.id) or self._build_plan(job, data, parent, current.video_seconds)
            self._check_plan(job, generation, parent, files)
            for file in files:
                if file.role == "selection":
                    if any(entry["name"] == file.name and entry["id"] != file.id
                           for entry in self.writer.children(parent)):
                        raise PublishError("selection name became occupied after the generation was reserved")
                    self._create_or_verify(file, before_create=lambda: self._before_selection(job))
                else:
                    self._create_or_verify(file)

    def _before_selection(self, job: Job) -> None:
        self._parent(job)
        self.current(job)

    def _build_plan(self, job: Job, data: bytes, parent: str, video_seconds: float | None
                    ) -> tuple[int, list[PlannedFile]]:
        from . import adapter

        prefix = _prefix(job)
        destination = json.dumps([parent, prefix])
        occupied = [_generation(entry["name"], prefix) for entry in self.writer.children(parent)]
        generation = max([self.journal.highest_generation(destination), *(value or 0 for value in occupied)]) + 1
        if generation > MAX_GENERATION:
            raise PublishError("selection generation limit reached")
        video = adapter.Video(job.source.name, job.source.id, job.source.md5, video_seconds or job.asset.duration)
        doc = json.loads(data)
        provenance = {"provider": job.asset.provider, "course": job.asset.course, "lesson": job.asset.lesson_id,
                      "asset": job.asset.asset_id, "revision": job.source.revision, "run_id": doc["run"]["run_id"]}
        outputs = adapter.render(doc, video, generation, provenance)
        ids = dict(zip(ROLES, self.writer.reserve(len(ROLES)), strict=True))
        shas = {item.role: item.sha256 for item in outputs}
        outputs.append(adapter.selection(video, generation, {role: ids[role] for role in shas}, shas))
        files = [PlannedFile(item.role, ids[item.role], parent, item.name, item.data, item.sha256) for item in outputs]
        self._check_plan(job, generation, parent, files)
        self.journal.save_plan(job.id, destination, generation, files)
        return generation, files

    @staticmethod
    def _check_plan(job: Job, generation: int, parent: str, files: list[PlannedFile]) -> None:
        from .d2_pins import CONFIG8, PRODUCER_NAME

        if ([file.role for file in files] != list(ROLES) or not 1 <= generation <= MAX_GENERATION
                or not re.fullmatch(r"[a-f0-9]{32}", job.source.md5)):
            raise PublishError("invalid publication plan")
        number, prefix = str(generation).zfill(4), _prefix(job)
        for file in files:
            _file_id(file.id)
            if file.role == "selection":
                expected = f"{prefix}selected.g{number}.json"
            else:
                expected = f"{prefix}{PRODUCER_NAME}.{CONFIG8}-g{number}.{SUFFIXES[file.role]}"
            if (file.parent != parent or file.name != expected or "\0" in expected or "/" in expected
                    or len(file.data) > _limit(file.role) or hashlib.sha256(file.data).hexdigest() != file.sha256):
                raise PublishError("publication plan targets an invalid transcript or has corrupt bytes")

    def _create_or_verify(self, file: PlannedFile, before_create: Callable[[], None] | None = None) -> None:
        for _ in range(3):
            if self._verified(file):
                return
            if before_create is not None:
                before_create()
            try:
                self.writer.create(file)
            except Conflict:
                if self._verified(file):
                    return
                raise Retryable(f"{file.role} conflicted but could not yet be read back") from None
            except Retryable:
                pass
            if self._verified(file):
                return
        raise Retryable(f"{file.role} remained absent after three creates with its reserved ID")

    def _verified(self, file: PlannedFile) -> bool:
        meta = self.writer.metadata(file.id)
        if meta is None:
            return False
        if not self._matches(meta, file.id, file.parent, file.name, len(file.data)):
            raise PublishError(f"reserved {file.role} ID has a different parent, name or size")
        existing = self.writer.content(file.id, _limit(file.role))
        if existing is None or hashlib.sha256(existing).hexdigest() != file.sha256:
            raise PublishError(f"reserved {file.role} ID has different content")
        return True
