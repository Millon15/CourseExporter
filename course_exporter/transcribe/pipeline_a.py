"""Pipeline A: `gpt-4o-transcribe-diarize` over 20-minute chunks, stitched into one `uch-segments/1` document.

- Chunks overlap by 10 s. In the overlap, a segment of each chunk with similar words close in time is one
  utterance heard twice: the pair keeps one copy. Unpaired segments are split at a cut that no segment crosses,
  nearest the overlap's middle; one the other chunk does not cover at all is kept wherever it falls. A kept
  segment the other chunk contradicts (covers with different words), or kept from one side only past the cut,
  carries the `seam_unresolved` flag for the gate.
- The model labels speakers per request, so chunk n+1 gets up to four 2-10 s references, cut from earlier chunks'
  own output, named by the video-wide ids they stand for. A paired utterance also says who a label is: a label
  paired with one earlier speaker is that speaker. A label paired with two speakers, a reference name paired
  with another speaker, two labels paired into one speaker, and an unpaired new label while an earlier speaker
  has no 2 s reference all fail the item as ambiguous; so does a fifth speaker, since at most four carry across.
- Every raw response, request id and timing lands in `workdir` as the run's receipt; the course audio and text
  there stay private to the owner, since other users share the VPS.

The OpenAI key is read from the gateway's environment, never from argv, and is never written anywhere.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import segments as segments_doc
from .source import _duration, cut

MODEL = "gpt-4o-transcribe-diarize"
ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
PRODUCER_CONFIG = "a-4o-diarize-c20m-o10"
MAX_CHUNK_SECONDS = 1200.0
OVERLAP_SECONDS = 10.0
SEAM_TOLERANCE_SECONDS = 0.3
SEAM_FLAG = "seam_unresolved"
PAIR_SIMILARITY = 0.6
PAIR_DISTANCE_SECONDS = 1.5
REQUEST_ID = re.compile(r"req_[A-Za-z0-9]{8,64}")
ERROR_CODES = frozenset({"invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found",
                         "invalid_request_error", "server_error", "invalid_value", "unsupported_value",
                         "audio_too_long", "invalid_file_format", "context_length_exceeded"})
MAX_UPLOAD_BYTES = 24 * 1000 * 1000
MAX_REFERENCES = 4
REFERENCE_SECONDS = (2.0, 10.0)
PRICE_PER_MINUTE = 0.006
PRICE_PER_TOKEN = {"input_tokens": 2.50e-6, "output_tokens": 10.00e-6}
REQUEST_TIMEOUT_SECONDS = 900
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS = (5, 30, 120)
GATEWAY_UNIT = "openclaw-gateway.service"

Opener = Callable[..., Any]


class PipelineError(Exception):
    """The item cannot produce a trustworthy transcript; the message names why."""


@dataclass(frozen=True)
class Clip:
    audio: Path
    clip_id: str
    language: str
    offset: float = 0.0


@dataclass(frozen=True)
class Window:
    index: int
    start: float
    end: float
    previous_end: float


@dataclass
class Receipt:
    request_ids: list[str] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)
    api_seconds: float = 0.0
    retries: int = 0


def plan(duration: float, max_seconds: float = MAX_CHUNK_SECONDS, overlap: float = OVERLAP_SECONDS) -> list[Window]:
    """Windows of at most `max_seconds`, each after the first starting `overlap` before the previous one ends."""
    starts = [0.0]
    while starts[-1] + max_seconds < duration:
        starts.append(starts[-1] + max_seconds - overlap)
    ends = [min(start + max_seconds, duration) for start in starts]
    return [Window(i, start, end, ends[i - 1] if i else 0.0) for i, (start, end) in enumerate(zip(starts, ends))]


class Transcriber:
    def __init__(self, key: str, workdir: Path, opener: Opener = urllib.request.urlopen, ffmpeg: str = "ffmpeg",
                 sleep: Callable[[float], None] = time.sleep, max_chunk_seconds: float = MAX_CHUNK_SECONDS) -> None:
        self._key, self._workdir, self._opener, self._ffmpeg, self._sleep = key, workdir, opener, ffmpeg, sleep
        self._max_chunk_seconds = max_chunk_seconds

    def transcribe(self, clip: Clip) -> dict[str, Any]:
        began = time.monotonic()
        self._workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._workdir.chmod(0o700)
        duration = _duration(self._ffmpeg, clip.audio)
        receipt, kept, voices, known = Receipt(), [], {}, []
        for window in plan(duration, self._max_chunk_seconds):
            response = self._request(clip, window, voices, receipt)
            incoming = _placed(response, window)
            pairs = _seam_pairs(kept, incoming, window)
            labels = _label_map(incoming, kept, pairs, voices, known, window)
            known = list(dict.fromkeys([*known, *labels.values()]))
            kept = _stitch(kept, [{**s, "speaker": labels[s["speaker"]]} for s in incoming], pairs, window)
            voices = self._references(clip, kept, voices)
        merge_began = time.monotonic()
        document = self._document(clip, duration, _renumbered(kept), receipt)
        document["run"]["stages_s"].update(merge=round(time.monotonic() - merge_began, 3),
                                           total=round(time.monotonic() - began, 3))
        segments_doc.validate(document)
        return document

    def _request(self, clip: Clip, window: Window, voices: dict[str, Path], receipt: Receipt) -> dict[str, Any]:
        chunk = cut(clip.audio, window.start, window.end - window.start,
                    self._workdir / f"{clip.clip_id}.chunk{window.index:02d}.ogg", self._ffmpeg)
        if chunk.stat().st_size > MAX_UPLOAD_BYTES:
            raise PipelineError(f"chunk {window.index} is {chunk.stat().st_size} bytes, over {MAX_UPLOAD_BYTES}")
        fields = [("model", MODEL), ("response_format", "diarized_json"), ("chunking_strategy", "auto"),
                  ("language", clip.language)]
        for name, reference in voices.items():
            fields += [("known_speaker_names[]", name), ("known_speaker_references[]", _data_url(reference))]
        response = self._post(fields, chunk, receipt)
        self._save(f"{clip.clip_id}.chunk{window.index:02d}.response.json",
                   {"window": window.__dict__, "references": sorted(voices), **response})
        return response

    def _post(self, fields: list[tuple[str, str]], chunk: Path, receipt: Receipt) -> dict[str, Any]:
        body, content_type = _multipart(fields, chunk)
        for delay in (*RETRY_DELAYS, None):
            request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": f"Bearer {self._key}", "Content-Type": content_type})
            began = time.monotonic()
            try:
                with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
                    payload = json.load(reply)
                    receipt.request_ids.append(_request_id(reply.headers.get("x-request-id")))
            except urllib.error.HTTPError as exc:
                request_id = _request_id(exc.headers.get("x-request-id") if exc.headers else None)
                receipt.request_ids.append(request_id)
                if exc.code not in RETRY_STATUSES or delay is None:
                    report = f"OpenAI HTTP {exc.code}, request {request_id}, error code {_error_code(exc)}"
                    raise PipelineError(report.replace(self._key, "<key>")) from None
                receipt.retries += 1
                self._sleep(delay)
                continue
            finally:
                receipt.api_seconds += time.monotonic() - began
            receipt.usage.append(payload.get("usage") or {})
            return payload
        raise AssertionError("unreachable")

    def _references(self, clip: Clip, kept: list[dict[str, Any]], voices: dict[str, Path]) -> dict[str, Path]:
        speakers = list(dict.fromkeys(segment["speaker"] for segment in kept))
        if len(speakers) > MAX_REFERENCES:
            raise PipelineError(f"{len(speakers)} speakers before a seam; at most {MAX_REFERENCES} carry across")
        references = dict(voices)
        for speaker in speakers:
            span = _reference_span([s for s in kept if s["speaker"] == speaker])
            if span and speaker not in references:
                path = self._workdir / f"{clip.clip_id}.ref-{speaker}.wav"
                references[speaker] = _wav(clip.audio, span, path, self._ffmpeg)
        return references

    def _document(self, clip: Clip, duration: float, kept: list[dict[str, Any]], receipt: Receipt) -> dict[str, Any]:
        self._save(f"{clip.clip_id}.receipt.json", receipt.__dict__)
        return {
            "schema": segments_doc.SCHEMA, "pipeline": "A", "producer_config": PRODUCER_CONFIG,
            "language": clip.language, "speakers": list(dict.fromkeys(s["speaker"] for s in kept)),
            "source": {"clip_id": clip.clip_id, "audio_sha256": _sha256(clip.audio), "offset_s": clip.offset,
                       "duration_s": round(duration, 3)},
            "segments": kept,
            "run": {"run_id": uuid.uuid4().hex, "host": socket.gethostname(), "warm": False, "peak_rss_mb": None,
                    "stages_s": {"api": round(receipt.api_seconds, 3)}, "request_ids": receipt.request_ids,
                    "usage": _summed(receipt.usage), "cost_usd": _cost(receipt.usage)},
        }

    def _save(self, name: str, payload: dict[str, Any]) -> None:
        (self._workdir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _placed(response: dict[str, Any], window: Window) -> list[dict[str, Any]]:
    """Every non-empty segment of the window, video-relative, still under the model's own label."""
    return [{"start": round(window.start + float(raw["start"]), 3), "end": round(window.start + float(raw["end"]), 3),
             "speaker": raw["speaker"], "text": raw["text"].strip(), "flags": []}
            for raw in response["segments"] if raw["text"].strip()]


def _seam_pairs(kept: list[dict[str, Any]], incoming: list[dict[str, Any]], window: Window) -> list[tuple[int, int]]:
    """(kept index, incoming index) of overlap segments that are one utterance: similar words, close in time."""
    if window.index == 0:
        return []
    olds = [(i, s) for i, s in enumerate(kept) if s["end"] > window.start]
    news = [(j, s) for j, s in enumerate(incoming) if s["start"] < window.previous_end]
    scored = sorted(((_similarity(a, b), i, j) for i, a in olds for j, b in news if _close(a, b)), reverse=True)
    pairs, used_old, used_new = [], set(), set()
    for score, i, j in scored:
        if score >= PAIR_SIMILARITY and i not in used_old and j not in used_new:
            pairs.append((i, j))
            used_old.add(i)
            used_new.add(j)
    return pairs


def _label_map(incoming: list[dict[str, Any]], kept: list[dict[str, Any]], pairs: list[tuple[int, int]],
               voices: dict[str, Path], known: list[str], window: Window) -> dict[str, str]:
    """Video-wide id per model label: its reference name, the one earlier speaker it pairs with, or a new id."""
    heard: dict[str, set[str]] = {}
    for i, j in pairs:
        heard.setdefault(incoming[j]["speaker"], set()).add(kept[i]["speaker"])
    unreferenced = _unreferenced(kept, voices)
    labels: dict[str, str] = {}
    for label in dict.fromkeys(s["speaker"] for s in incoming):
        labels[label] = _identify(label, heard.get(label, set()), voices, unreferenced,
                                  len(known) + sum(1 for v in labels.values() if v not in known) + 1, window)
    doubled = [v for v in set(labels.values()) if list(labels.values()).count(v) > 1]
    if doubled:
        raise PipelineError(f"window {window.index}: two labels pair with {', '.join(sorted(doubled))}; "
                            "the speaker mapping is ambiguous")
    return labels


def _identify(label: str, heard: set[str], voices: dict[str, Path], unreferenced: list[str], next_id: int,
              window: Window) -> str:
    if label in voices and heard - {label}:
        raise PipelineError(f"window {window.index}: reference {label} pairs with {', '.join(sorted(heard))}; "
                            "the speaker mapping is ambiguous")
    if label in voices:
        return label
    if len(heard) > 1:
        raise PipelineError(f"window {window.index}: label {label!r} pairs with {', '.join(sorted(heard))}; "
                            "the speaker mapping is ambiguous")
    if heard:
        return next(iter(heard))
    if unreferenced:
        raise PipelineError(f"window {window.index}: label {label!r} may be {', '.join(unreferenced)}, "
                            "who had no 2 s reference; the speaker mapping is ambiguous")
    return f"S{next_id}"


def _unreferenced(kept: list[dict[str, Any]], voices: dict[str, Path]) -> list[str]:
    return [speaker for speaker in dict.fromkeys(s["speaker"] for s in kept) if speaker not in voices]


def _stitch(kept: list[dict[str, Any]], incoming: list[dict[str, Any]], pairs: list[tuple[int, int]],
            window: Window) -> list[dict[str, Any]]:
    """Joins a window to what came before; see the module docstring for the seam rules."""
    if window.index == 0:
        return kept + incoming
    low, high = window.start, window.previous_end
    paired_old, paired_new = {i for i, _ in pairs}, {j for _, j in pairs}
    loose_old = [s for i, s in enumerate(kept) if i not in paired_old and s["end"] > low]
    loose_new = [s for j, s in enumerate(incoming) if j not in paired_new and s["start"] < high]
    cut = _common_gap(loose_old + loose_new, low, high)
    cut = (low + high) / 2 if cut is None else cut
    loose = {id(s) for s in loose_old + loose_new}
    joined = [_seam_side(s, loose_new, cut, before=True) if id(s) in loose else s
              for i, s in enumerate(kept) if i not in paired_old]
    joined += [_seam_side(s, loose_old, cut, before=False) if id(s) in loose else s
               for j, s in enumerate(incoming) if j not in paired_new]
    joined += [kept[i] if (_middle(kept[i]) + _middle(incoming[j])) / 2 < cut else incoming[j] for i, j in pairs]
    return [s for s in joined if s is not None]


def _seam_side(segment: dict[str, Any], others: list[dict[str, Any]], cut: float,
               before: bool) -> dict[str, Any] | None:
    """An unpaired overlap segment: kept on its side of the cut, or anywhere when the other chunk is silent there."""
    contested = any(_overlaps(segment, other) for other in others)
    on_its_side = (_middle(segment) < cut) == before
    if on_its_side and not contested:
        return segment
    if on_its_side or not contested:
        return {**segment, "flags": [*segment["flags"], SEAM_FLAG]}
    return None


def _common_gap(near: list[dict[str, Any]], low: float, high: float) -> float | None:
    """The segment edge in [low, high], nearest the middle, that no segment crosses by more than the tolerance."""
    edges = {low, high, *(t for s in near for t in (s["start"], s["end"]) if low <= t <= high)}
    middle, tolerance = (low + high) / 2, SEAM_TOLERANCE_SECONDS
    open_cuts = [t for t in edges if not any(s["start"] + tolerance < t < s["end"] - tolerance for s in near)]
    return min(open_cuts, key=lambda t: (abs(t - middle), t)) if open_cuts else None


def _similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    return difflib.SequenceMatcher(None, _words(a["text"]), _words(b["text"])).ratio()


def _close(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _overlaps(a, b) or abs(_middle(a) - _middle(b)) <= PAIR_DISTANCE_SECONDS


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _middle(segment: dict[str, Any]) -> float:
    return (segment["start"] + segment["end"]) / 2


def _renumbered(kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(kept, key=lambda s: s["start"])
    ids = {speaker: f"S{n}" for n, speaker in enumerate(dict.fromkeys(s["speaker"] for s in ordered), start=1)}
    return [{**segment, "speaker": ids[segment["speaker"]]} for segment in ordered]


def _error_code(exc: urllib.error.HTTPError) -> str:
    """The provider's error code when it is one we know; its message and any other string stay out of reports."""
    try:
        error = json.loads(exc.read(4000)).get("error") or {}
    except (ValueError, AttributeError, OSError):
        return "unreadable"
    for code in (error.get("code"), error.get("type")):
        if code in ERROR_CODES:
            return code
    return "other"


def _request_id(value: str | None) -> str:
    return value if value and REQUEST_ID.fullmatch(value) else "invalid"


def _reference_span(own: list[dict[str, Any]]) -> tuple[float, float] | None:
    """The longest of a speaker's segments, trimmed to 10 s; none when every segment is under 2 s."""
    low, high = REFERENCE_SECONDS
    longest = max(own, key=lambda s: s["end"] - s["start"])
    length = min(longest["end"] - longest["start"], high)
    return (longest["start"], length) if length >= low else None


def _wav(audio: Path, span: tuple[float, float], out: Path, ffmpeg: str) -> Path:
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{span[0]:.3f}",
                    "-i", str(audio), "-t", f"{span[1]:.3f}", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                    str(out)], check=True, timeout=120)
    return out


def _data_url(wav: Path) -> str:
    return "data:audio/wav;base64," + base64.b64encode(wav.read_bytes()).decode()


def _multipart(fields: list[tuple[str, str]], chunk: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in fields]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{chunk.name}"\r\n'
                 f'Content-Type: audio/ogg\r\n\r\n'.encode() + chunk.read_bytes() + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _summed(usage: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {"requests": len(usage), "types": sorted({u.get("type", "") for u in usage})}
    for key in ("seconds", "input_tokens", "output_tokens", "total_tokens"):
        totals[key] = sum(u.get(key, 0) for u in usage)
    return totals


def _cost(usage: list[dict[str, Any]]) -> float:
    """Token usage at the listed token prices; duration usage at the listed per-minute price."""
    tokens = sum(u.get(key, 0) * price for u in usage if u.get("type") == "tokens"
                 for key, price in PRICE_PER_TOKEN.items())
    minutes = sum(u.get("seconds", 0) for u in usage if u.get("type") == "duration") / 60
    return round(tokens + minutes * PRICE_PER_MINUTE, 6)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gateway_key(systemctl: str = "systemctl") -> str:
    """`OPENAI_API_KEY` from the running gateway's environment, the way the Radio-T pipeline reads it."""
    result = subprocess.run([systemctl, "--user", "show", "-p", "MainPID", "--value", GATEWAY_UNIT],
                            capture_output=True, text=True, timeout=30)
    pid = result.stdout.strip()
    if result.returncode != 0 or not pid.isdigit() or pid == "0":
        raise PipelineError(f"could not resolve the {GATEWAY_UNIT} MainPID")
    for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if entry.startswith(b"OPENAI_API_KEY=") and len(entry) > len(b"OPENAI_API_KEY="):
            return entry.split(b"=", 1)[1].decode()
    raise PipelineError("OPENAI_API_KEY is absent from the gateway environment")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m course_exporter.transcribe.pipeline_a")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--language", choices=sorted(segments_doc.LANGUAGES), required=True)
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--workdir", type=Path, required=True, help="raw responses, references and the receipt")
    args = parser.parse_args(argv)
    try:
        transcriber = Transcriber(gateway_key(), args.workdir)
        document = transcriber.transcribe(Clip(args.audio, args.clip_id, args.language, args.offset))
    except PipelineError as exc:
        print(f"pipeline_a: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
