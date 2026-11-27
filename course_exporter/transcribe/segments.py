"""The `uch-segments/1` document both pipelines emit, its validation, and the `vtt` and `txt` renderings of it.

Times are seconds relative to the audio the pipeline read; `source.offset_s` places that audio in the video.
Speaker ids are `S1..Sn` in order of first appearance, never names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "uch-segments/1"
PIPELINES = frozenset({"A", "D"})
LANGUAGES = frozenset({"ru", "uk"})
WORD_FLAGS = frozenset({None, "no_turn", "ambiguous"})
STAGES = frozenset({"load", "asr", "diarize", "merge", "upload", "api", "total"})
END_SLACK_SECONDS = 0.5
SHA256 = re.compile(r"[0-9a-f]{64}")
SPEAKER = re.compile(r"S[1-9][0-9]*")


def validate(doc: dict[str, Any]) -> None:
    """Raises `ValueError` naming the first rule the document breaks."""
    _require(isinstance(doc, dict), "document is not an object")
    _require(doc.get("schema") == SCHEMA, f"schema is not {SCHEMA}")
    _require(doc.get("pipeline") in PIPELINES, "pipeline is not A or D")
    _require(_is_text(doc.get("producer_config")), "producer_config is empty")
    _require(doc.get("language") in LANGUAGES, "language is not ru or uk")
    duration = _validate_source(doc.get("source"))
    _validate_segments(doc.get("segments"), duration)
    _validate_speakers(doc.get("speakers"), doc["segments"])
    _validate_run(doc.get("run"))


def write(doc: dict[str, Any], directory: Path, stem: str) -> list[Path]:
    """Writes `<stem>.segments.json`, `<stem>.vtt` and `<stem>.txt` for a valid document."""
    validate(doc)
    outputs = {
        directory / f"{stem}.segments.json": json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
        directory / f"{stem}.vtt": to_vtt(doc),
        directory / f"{stem}.txt": to_txt(doc),
    }
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    return list(outputs)


def to_vtt(doc: dict[str, Any]) -> str:
    cues = [_vtt_cue(segment) for segment in doc["segments"]]
    return "WEBVTT\n\n" + "\n".join(cues)


def to_txt(doc: dict[str, Any]) -> str:
    return "".join(f"[{_clock(s['start'])}] {s['speaker'] or '?'}: {s['text']}\n" for s in doc["segments"])


def _validate_source(source: Any) -> float:
    _require(isinstance(source, dict), "source is not an object")
    _require(_is_text(source.get("clip_id")), "source.clip_id is empty")
    _require(SHA256.fullmatch(str(source.get("audio_sha256"))) is not None, "source.audio_sha256 is not sha256 hex")
    _require(_is_number(source.get("offset_s")) and source["offset_s"] >= 0, "source.offset_s is not >= 0")
    _require(_is_number(source.get("duration_s")) and source["duration_s"] > 0, "source.duration_s is not > 0")
    return float(source["duration_s"])


def _validate_segments(segments: Any, duration: float) -> None:
    _require(isinstance(segments, list), "segments is not a list")
    previous_start = 0.0
    for index, segment in enumerate(segments):
        _validate_span(segment, duration, f"segments[{index}]")
        _require(segment["start"] >= previous_start, f"segments[{index}] starts before the previous segment")
        _require(_is_text(segment.get("text")), f"segments[{index}].text is empty")
        _require(isinstance(segment.get("flags"), list), f"segments[{index}].flags is not a list")
        _validate_words(segment, duration, f"segments[{index}]")
        previous_start = segment["start"]


def _validate_words(segment: dict[str, Any], duration: float, where: str) -> None:
    words = segment.get("words")
    if words is None:
        _require(segment.get("speaker") is not None, f"{where}.speaker is null without flagged words")
        return
    _require(isinstance(words, list) and words, f"{where}.words is not a non-empty list")
    for index, word in enumerate(words):
        _validate_span(word, duration, f"{where}.words[{index}]")
        _require(isinstance(word.get("text"), str), f"{where}.words[{index}].text is not a string")
        _require(word.get("flag") in WORD_FLAGS, f"{where}.words[{index}].flag is unknown")
        _require(word.get("speaker") is not None or word["flag"] is not None,
                 f"{where}.words[{index}] has no speaker and no flag")
    _require(segment.get("speaker") is not None or all(word["flag"] for word in words),
             f"{where}.speaker is null but a word is unflagged")


def _validate_span(span: Any, duration: float, where: str) -> None:
    _require(isinstance(span, dict), f"{where} is not an object")
    _require(_is_number(span.get("start")) and _is_number(span.get("end")), f"{where} start/end is not a number")
    _require(0 <= span["start"] <= span["end"], f"{where} does not satisfy 0 <= start <= end")
    _require(span["end"] <= duration + END_SLACK_SECONDS, f"{where} ends after the audio")


def _validate_speakers(speakers: Any, segments: list[dict[str, Any]]) -> None:
    _require(isinstance(speakers, list), "speakers is not a list")
    _require(all(isinstance(s, str) and SPEAKER.fullmatch(s) for s in speakers), "speakers are not S1..Sn ids")
    expected = list(dict.fromkeys(_speakers_in_order(segments)))
    _require(speakers == expected, f"speakers {speakers} are not first-appearance order {expected}")
    _require(speakers == [f"S{n}" for n in range(1, len(speakers) + 1)], "speakers are not numbered S1..Sn in order")


def _speakers_in_order(segments: list[dict[str, Any]]) -> list[str]:
    spoken = []
    for segment in segments:
        spoken.append(segment.get("speaker"))
        spoken.extend(word.get("speaker") for word in segment.get("words") or [])
    return [speaker for speaker in spoken if speaker is not None]


def _validate_run(run: Any) -> None:
    _require(isinstance(run, dict), "run is not an object")
    _require(_is_text(run.get("run_id")) and _is_text(run.get("host")), "run.run_id or run.host is empty")
    _require(isinstance(run.get("warm"), bool), "run.warm is not a boolean")
    _require(_is_optional_number(run.get("peak_rss_mb")), "run.peak_rss_mb is not a number or null")
    _require(_is_optional_number(run.get("cost_usd")), "run.cost_usd is not a number or null")
    _require(run.get("usage") is None or isinstance(run["usage"], dict), "run.usage is not an object or null")
    _require(isinstance(run.get("request_ids"), list) and all(_is_text(r) for r in run["request_ids"]),
             "run.request_ids is not a list of ids")
    stages = run.get("stages_s")
    _require(isinstance(stages, dict) and "total" in stages, "run.stages_s has no total")
    _require(set(stages) <= STAGES, f"run.stages_s has unknown keys {sorted(set(stages) - STAGES)}")
    _require(all(_is_number(v) and v >= 0 for v in stages.values()), "run.stages_s holds a negative or non-number")


def _vtt_cue(segment: dict[str, Any]) -> str:
    text = _vtt_escape(segment["text"])
    body = f"<v {segment['speaker']}>{text}</v>" if segment["speaker"] else text
    return f"{_vtt_time(segment['start'])} --> {_vtt_time(segment['end'])}\n{body}\n"


def _vtt_escape(text: str) -> str:
    return " ".join(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").split())


def _vtt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    return f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_optional_number(value: Any) -> bool:
    return value is None or _is_number(value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
