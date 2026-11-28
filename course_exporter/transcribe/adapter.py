"""Turns a validated D2 `uch-segments/1` document into the files UniversalCourseHost reads beside the video.

Contract: UCH `src/importer/transcripts.ts` (`selectTranscript`, `readCandidate`, `readSegments`).

- Names: `<video name without .mp4>.<md5>.` then `d2-nemotron3-parakeet3.<config8>-g<NNNN>` and `.vtt`, `.txt`,
  `.segments.json`, `.manifest.json`; the selection is `<prefix>selected.g<NNNN>.json`.
- Times are whole milliseconds of video time, so `segments.json` and the VTT agree to the millisecond.
- A null speaker becomes `?`. A cue that rounding leaves empty (one zero-length word) joins the next cue when that
  starts at the same millisecond and its own speaker is unknown or the same, taking the next cue's span and speaker;
  otherwise it widens by at most 0.2 s, never past the next cue's start or the video's end. One still empty refuses
  the whole item. The manifest's provenance counts both repairs. v13 decision 8 refused every empty cue; this
  repair keeps the text (bench ru-dialogue 857.76 s: unknown "То" + S1 "есть, типа.").
- Pure: no I/O and no Drive; the publisher names, uploads and selects.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any

from . import d2_pins as pins

ROLES = ("vtt", "text", "segments")
SELECTION_KEYS = frozenset({*ROLES, "manifest"})
MAX_OUTPUT_BYTES = 8 << 20
MAX_METADATA_BYTES = 128 << 10
MAX_GENERATION = 2_147_483_647
MAX_SPEAKER_CHARS = 128
END_SLACK_MS = 10
WIDEN_MS = 200
UNKNOWN_SPEAKER = "?"
MD5 = re.compile(r"[0-9a-f]{32}")
MP4_SUFFIX = re.compile(r"\.mp4$", re.IGNORECASE)


class Refused(ValueError):
    """The document cannot become a transcript UCH accepts; the item fails with this reason."""


@dataclass(frozen=True)
class Video:
    name: str
    drive_file_id: str
    md5: str
    duration_s: float

    @property
    def prefix(self) -> str:
        return f"{MP4_SUFFIX.sub('', self.name)}.{self.md5}."


@dataclass(frozen=True)
class OutputFile:
    role: str
    name: str
    data: bytes
    sha256: str

    @classmethod
    def of(cls, role: str, name: str, data: bytes) -> OutputFile:
        return cls(role, name, data, hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    speaker: str
    text: str


def render(doc: dict[str, Any], video: Video, generation: int, provenance: dict[str, Any]) -> list[OutputFile]:
    """The vtt, text, segments and manifest files, in the order the publisher creates them."""
    _require_video(video)
    _require_generation(generation)
    cues, repairs = _cues(doc, video)
    stem = f"{video.prefix}{pins.PRODUCER_NAME}.{pins.CONFIG8}-g{_padded(generation)}"
    outputs = [
        OutputFile.of("vtt", f"{stem}.vtt", _vtt(cues)),
        OutputFile.of("text", f"{stem}.txt", _text(cues)),
        OutputFile.of("segments", f"{stem}.segments.json", _segments_json(cues)),
    ]
    _require_sizes(outputs, MAX_OUTPUT_BYTES)
    manifest = OutputFile.of("manifest", f"{stem}.manifest.json",
                             _manifest(doc, video, outputs, provenance | {"cue_repairs": repairs}))
    _require_sizes([manifest], MAX_METADATA_BYTES)
    return [*outputs, manifest]


def selection(video: Video, generation: int, ids: dict[str, str], shas: dict[str, str]) -> OutputFile:
    """The selection file that makes one generation current; written last, after every file it names exists."""
    _require_video(video)
    _require_generation(generation)
    _require(set(ids) == SELECTION_KEYS and set(shas) == SELECTION_KEYS, "selection needs vtt, text, segments, manifest")
    _require(all(isinstance(v, str) and v.strip() for v in ids.values()), "a selection id is empty")
    _require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in shas.values()),
             "a selection sha256 is not hex")
    ref = {role: {"id": ids[role], "sha256": shas[role]} for role in SELECTION_KEYS}
    data = _json({
        **_revision(video), "generation": generation, "manifest": ref["manifest"],
        "outputs": {role: ref[role] for role in ROLES},
    })
    file = OutputFile.of("selection", f"{video.prefix}selected.g{_padded(generation)}.json", data)
    _require_sizes([file], MAX_METADATA_BYTES)
    return file


def selection_pattern(video: Video) -> re.Pattern[str]:
    """Matches this revision's selection names; group 1 holds the generation digits as UCH reads them."""
    return re.compile(re.escape(f"{video.prefix}selected.g") + r"(\d{4,10})\.json")


def _cues(doc: dict[str, Any], video: Video) -> tuple[list[Cue], dict[str, int]]:
    offset_ms = round(doc["source"]["offset_s"] * 1000)
    limit_ms = int(video.duration_s * 1000) + END_SLACK_MS
    raw = [_cue(segment, offset_ms) for segment in doc["segments"]]
    cues, repairs = _repaired(raw, limit_ms)
    for index, cue in enumerate(cues):
        _require(cue.end_ms > cue.start_ms, f"cue {index} at {cue.start_ms} ms is empty after millisecond rounding")
        _require(cue.end_ms <= limit_ms, f"cue {index} ends at {cue.end_ms} ms, after the video ({limit_ms} ms)")
    return cues, repairs


def _cue(segment: dict[str, Any], offset_ms: int) -> Cue:
    speaker = segment["speaker"] or UNKNOWN_SPEAKER
    text = segment["text"]
    _require(isinstance(text, str) and text.strip() != "", "a cue has blank text")
    _require("\0" not in text and "\0" not in speaker, "a cue holds a NUL character")
    _require(len(speaker) <= MAX_SPEAKER_CHARS, "a speaker is longer than 128 characters")
    return Cue(offset_ms + round(segment["start"] * 1000), offset_ms + round(segment["end"] * 1000), speaker, text)


def _repaired(raw: list[Cue], limit_ms: int) -> tuple[list[Cue], dict[str, int]]:
    repaired: list[Cue] = []
    repairs = {"joined": 0, "widened": 0}
    carried = ""
    for index, cue in enumerate(raw):
        cue = replace(cue, text=f"{carried} {cue.text}") if carried else cue
        following = raw[index + 1:index + 2]
        carried = cue.text if _joins_next(cue, following) else ""
        if carried:
            repairs["joined"] += 1
            continue
        widened = _widened(cue, following, limit_ms)
        repairs["widened"] += widened.end_ms != cue.end_ms
        repaired.append(widened)
    return repaired, repairs


def _joins_next(cue: Cue, following: list[Cue]) -> bool:
    """An empty cue joins a next cue at the same millisecond only when that cannot reassign a known speaker."""
    return (cue.end_ms <= cue.start_ms and bool(following) and following[0].start_ms == cue.start_ms
            and cue.speaker in (UNKNOWN_SPEAKER, following[0].speaker))


def _widened(cue: Cue, following: list[Cue], limit_ms: int) -> Cue:
    if cue.end_ms > cue.start_ms:
        return cue
    bounds = [cue.start_ms + WIDEN_MS, limit_ms]
    bounds += [after.start_ms for after in following]
    return Cue(cue.start_ms, min(bounds), cue.speaker, cue.text)


def _vtt(cues: list[Cue]) -> bytes:
    blocks = [f"{_vtt_time(c.start_ms)} --> {_vtt_time(c.end_ms)}\n<v {c.speaker}>{_vtt_escape(c.text)}</v>\n"
              for c in cues]
    return ("WEBVTT\n\n" + "\n".join(blocks)).encode()


def _text(cues: list[Cue]) -> bytes:
    return "".join(f"[{_clock(c.start_ms)}] {c.speaker}: {c.text}\n" for c in cues).encode()


def _segments_json(cues: list[Cue]) -> bytes:
    return _json({"schema_version": 1, "segments": [
        {"start": c.start_ms / 1000, "end": c.end_ms / 1000, "speaker": c.speaker, "text": c.text} for c in cues
    ]})


def _manifest(doc: dict[str, Any], video: Video, outputs: list[OutputFile], provenance: dict[str, Any]) -> bytes:
    return _json({
        **_revision(video),
        "language": doc["language"],
        "timing_kind": pins.TIMING_KIND,
        "speaker_method": pins.SPEAKER_METHOD,
        "output_sha256s": {file.role: file.sha256 for file in outputs},
        "provenance": {
            **provenance,
            "pipeline": doc["pipeline"],
            "producer_config": doc["producer_config"],
            "result_version": doc["result_version"],
            "run_id": doc["run"]["run_id"],
            "models": {pins.NEMOTRON: pins.NEMOTRON_REV, pins.PARAKEET: pins.PARAKEET_REV},
            "word_timing": "parakeet-tdt-0.6b-v3 tokens",
        },
    })


def _revision(video: Video) -> dict[str, Any]:
    return {"schema_version": 1, "drive_file_id": video.drive_file_id, "source_md5": video.md5}


def _json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _vtt_escape(text: str) -> str:
    return " ".join(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").split())


def _vtt_time(ms: int) -> str:
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _padded(generation: int) -> str:
    return str(generation).zfill(4)


def _require_video(video: Video) -> None:
    _require(MD5.fullmatch(video.md5) is not None, "video md5 is not lowercase hex")
    _require(video.drive_file_id.strip() != "", "video drive_file_id is empty")
    _require(video.duration_s > 0, "video duration is not positive")
    _require(MP4_SUFFIX.sub("", video.name).strip() != "", "video name has no stem")


def _require_generation(generation: int) -> None:
    _require(isinstance(generation, int) and 1 <= generation <= MAX_GENERATION, "generation is out of range")


def _require_sizes(files: list[OutputFile], limit: int) -> None:
    for file in files:
        _require(len(file.data) <= limit, f"{file.role} is {len(file.data)} bytes, over {limit}")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refused(reason)
