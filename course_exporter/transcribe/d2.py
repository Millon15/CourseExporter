"""Pipeline D2 on the Mac: Parakeet v3 (MLX) words, Nemotron 3 Diarization speakers, pipeline D's word join.

- ASR: Parakeet reads the Opus file itself in 120 s chunks with 15 s overlap; its subword tokens join into words.
- Diarization: Nemotron's offline mode gives per-frame speaker probabilities; a frame goes to its most probable
  speaker when that probability passes 0.5, so overlapped speech keeps one speaker.
- Join: a word takes the speaker whose turn it overlaps; none or several flag it. A cue breaks on a speaker or flag
  change, sentence punctuation, a 0.8 s pause, or 5 s of length.

The heavy libraries load inside the functions that need them, so the pure parts import anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import d2_pins as pins
from . import segments

EXIT_MODELS_MISSING = 3


class ModelUnavailable(Exception):
    """A pinned model is missing from the local Hugging Face cache."""


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str


@dataclass(frozen=True)
class Models:
    processor: Any
    diarizer: Any
    recognizer: Any


def fetch() -> None:
    """Downloads the pinned model files once; every later run is offline."""
    from huggingface_hub import snapshot_download

    snapshot_download(pins.NEMOTRON, revision=pins.NEMOTRON_REV, allow_patterns=list(pins.NEMOTRON_FILES))
    snapshot_download(pins.PARAKEET, revision=pins.PARAKEET_REV, allow_patterns=list(pins.PARAKEET_FILES))


def load_models() -> Models:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    nemotron = _local_snapshot(pins.NEMOTRON, pins.NEMOTRON_REV, pins.NEMOTRON_FILES)
    parakeet = _local_snapshot(pins.PARAKEET, pins.PARAKEET_REV, pins.PARAKEET_FILES)
    import mlx.core as mx
    import torch
    from parakeet_mlx import from_pretrained
    from transformers import AutoModelForAudioFrameClassification, AutoProcessor

    processor = AutoProcessor.from_pretrained(nemotron)
    diarizer = AutoModelForAudioFrameClassification.from_pretrained(nemotron, dtype=torch.float32)
    recognizer = from_pretrained(str(parakeet), dtype=mx.bfloat16)
    mx.eval(recognizer.parameters())
    return Models(processor, diarizer.to(pins.DEVICE).eval(), recognizer)


def transcribe(models: Models, audio: Path, language: str, job_id: str) -> dict[str, Any]:
    """One full-video `uch-segments/1` D2 document for an Ogg Opus file."""
    started = time.perf_counter()
    audio = audio.resolve(strict=True)
    audio_sha = _sha256(audio)
    samples = _decode_opus(audio)
    duration = len(samples) / (4 * pins.SAMPLE_RATE)
    tokens = _recognize(models, audio)
    asr_done = time.perf_counter()
    turns = _diarize(models, samples)
    diarize_done = time.perf_counter()
    speakers, cues = join_segments(words_from_tokens(tokens, duration), turns)
    doc = _document(language, job_id, audio_sha, duration, speakers, cues, {
        "asr": asr_done - started,
        "diarize": diarize_done - asr_done,
        "merge": time.perf_counter() - diarize_done,
        "total": time.perf_counter() - started,
    })
    segments.validate(doc)
    return doc


def words_from_tokens(tokens: Iterable[Word], duration: float) -> list[Word]:
    """Joins Parakeet's whitespace-delimited subword pieces, attached punctuation included."""
    words: list[Word] = []
    parts: list[str] = []
    start = end = 0.0
    for token in tokens:
        if not math.isfinite(token.start) or not math.isfinite(token.end) or token.end < token.start:
            raise ValueError("Parakeet returned an invalid token timestamp")
        token_start = min(max(0.0, token.start), duration)
        token_end = min(max(0.0, token.end), duration)
        for part in re.findall(r"\s+|\S+", token.text):
            if part.isspace():
                if parts:
                    words.append(Word(start, end, "".join(parts)))
                    parts.clear()
                continue
            if not parts:
                start, end = token_start, token_end
            parts.append(part)
            end = max(end, token_end)
    if parts:
        words.append(Word(start, end, "".join(parts)))
    return words


def exclusive_turns(best: list[float], speaker: list[int], frame_s: float) -> list[Turn]:
    """Runs of frames whose strongest speaker passes the threshold; `best[i]` is frame i's top probability."""
    labels = [f"spk{s}" if p > pins.THRESHOLD else None for p, s in zip(best, speaker)]
    turns, start = [], 0
    for index in range(1, len(labels) + 1):
        if index < len(labels) and labels[index] == labels[start]:
            continue
        label = labels[start]
        if label is not None:
            turns.append(Turn(start * frame_s, index * frame_s, label))
        start = index
    return turns


def join_segments(words: Iterable[Word], turns: Iterable[Turn]) -> tuple[list[str], list[dict]]:
    """Speaker ids `S1..Sn` in first-appearance order and the cues, each with its words."""
    ordered_turns = sorted(turns, key=lambda turn: turn.start)
    speaker_ids: dict[str, str] = {}
    cues: list[dict] = []
    current: list[dict] = []
    first_turn = 0
    cue_end = 0.0
    for word in sorted(words, key=lambda word: word.start):
        while first_turn < len(ordered_turns) and ordered_turns[first_turn].end <= word.start:
            first_turn += 1
        label, flag = _speaker_of(word, ordered_turns, first_turn)
        speaker = speaker_ids.setdefault(label, f"S{len(speaker_ids) + 1}") if label is not None else None
        if current and _breaks_cue(current, cue_end, word, speaker, flag):
            cues.append(_cue(current, cue_end))
            current = []
        cue_end = max(cue_end, word.end) if current else word.end
        current.append({"start": word.start, "end": word.end, "text": word.text, "speaker": speaker, "flag": flag})
    if current:
        cues.append(_cue(current, cue_end))
    return list(speaker_ids.values()), cues


def _breaks_cue(current: list[dict], cue_end: float, word: Word, speaker: str | None, flag: str | None) -> bool:
    last = current[-1]
    return ((speaker, flag) != (last["speaker"], last["flag"])
            or last["text"].endswith((".", "!", "?", "…"))
            or word.start - cue_end >= pins.SEGMENT_PAUSE_S
            or max(cue_end, word.end) - current[0]["start"] > pins.SEGMENT_MAX_S)


def _speaker_of(word: Word, turns: list[Turn], first: int) -> tuple[str | None, str | None]:
    speakers = set()
    for turn in turns[first:]:
        if turn.start > word.end:
            break
        if word.start == word.end:
            overlaps = turn.start <= word.start < turn.end
        else:
            overlaps = max(turn.start, word.start) < min(turn.end, word.end)
        if overlaps:
            speakers.add(turn.speaker)
        if len(speakers) > 1:
            return None, "ambiguous"
    if speakers:
        return speakers.pop(), None
    return None, "no_turn"


def _cue(words: list[dict], end: float) -> dict:
    first = words[0]
    return {
        "start": first["start"], "end": end, "speaker": first["speaker"],
        "text": " ".join(word["text"] for word in words),
        "flags": [first["flag"]] if first["flag"] else [],
        "words": words,
    }


def _recognize(models: Models, audio: Path) -> list[Word]:
    import mlx.core as mx

    result = models.recognizer.transcribe(
        audio, dtype=mx.bfloat16, chunk_duration=pins.ASR_CHUNK_S, overlap_duration=pins.ASR_OVERLAP_S,
    )
    return [Word(token.start, token.end, token.text) for sentence in result.sentences for token in sentence.tokens]


def _diarize(models: Models, samples: bytes) -> list[Turn]:
    import numpy as np
    import torch

    audio = np.frombuffer(samples, dtype=np.float32)
    inputs = models.processor(audio, sampling_rate=pins.SAMPLE_RATE).to(pins.DEVICE, dtype=torch.float32)
    with torch.inference_mode():
        probabilities = models.diarizer(**inputs).logits[0].sigmoid().cpu()
    best, speaker = probabilities.max(dim=-1)
    extractor = models.processor.feature_extractor
    return exclusive_turns(best.tolist(), speaker.tolist(), extractor.hop_length / extractor.sampling_rate)


def _document(language: str, job_id: str, audio_sha: str, duration: float, speakers: list[str], cues: list[dict],
              stages: dict[str, float]) -> dict[str, Any]:
    return {
        "schema": segments.SCHEMA,
        "pipeline": "D2",
        "result_version": pins.RESULT_VERSION,
        "producer_config": pins.PRODUCER_CONFIG,
        "language": language,
        "speakers": speakers,
        "source": {"clip_id": job_id, "audio_sha256": audio_sha, "offset_s": 0, "duration_s": duration},
        "segments": cues,
        "run": {
            "run_id": str(uuid.uuid4()),
            "host": f"{platform.system()} {platform.machine()}",
            "warm": False,
            "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1_000_000,
            "stages_s": stages,
            "request_ids": [],
            "usage": None,
            "cost_usd": None,
        },
    }


def _local_snapshot(repo: str, revision: str, files: tuple[str, ...]) -> Path:
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    hf_home = Path(os.environ.get("HF_HOME", xdg_cache / "huggingface")).expanduser()
    cache = Path(os.environ.get("HF_HUB_CACHE", os.environ.get("HUGGINGFACE_HUB_CACHE", hf_home / "hub")))
    snapshot = cache.expanduser() / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
    if not all((snapshot / name).is_file() for name in files):
        raise ModelUnavailable(f"{repo}@{revision} is not cached; run `python -m course_exporter.transcribe.d2 --fetch`")
    return snapshot


def _decode_opus(audio: Path) -> bytes:
    result = subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
         "-f", "ogg", "-i", str(audio), "-map", "0:a:0", "-ac", "1", "-ar", str(pins.SAMPLE_RATE),
         "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"FFmpeg could not decode the Ogg Opus input: {result.stderr.decode(errors='replace').strip()}")
    if not result.stdout or len(result.stdout) % 4:
        raise ValueError("FFmpeg returned no complete audio samples")
    return result.stdout


def _ffmpeg() -> str:
    """FFmpeg's path; Parakeet finds it on PATH too, so a Homebrew prefix outside a launchd PATH is added to it."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    brew_bin = Path.home() / ".brew" / "bin"
    if not (brew_bin / "ffmpeg").is_file():
        raise FileNotFoundError("FFmpeg is required to decode the audio")
    os.environ["PATH"] = str(brew_bin) + os.pathsep + os.environ.get("PATH", "")
    return str(brew_bin / "ffmpeg")


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_json(path: Path, doc: dict) -> None:
    payload = json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline D2: --fetch once, or transcribe one Opus file")
    parser.add_argument("--fetch", action="store_true", help="download the pinned model files, then exit")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--language", choices=sorted(segments.LANGUAGES))
    parser.add_argument("--job-id")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.fetch:
        fetch()
        return 0
    if not (args.audio and args.language and args.job_id and args.out):
        parser.error("--audio, --language, --job-id and --out are required")
    try:
        _write_json(args.out, transcribe(load_models(), args.audio, args.language, args.job_id))
    except ModelUnavailable as error:
        print(error, file=sys.stderr)
        return EXIT_MODELS_MISSING
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
