import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from course_exporter.transcribe import pipeline_a, segments

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg", path=os.path.expanduser("~/.brew/bin"))
KEY = "sk-SECRET"


class Reply:
    def __init__(self, payload, request_id):
        self.payload, self.headers = payload, {"x-request-id": request_id}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return json.dumps(self.payload).encode()


def seg(start, end, speaker, text="слово"):
    return {"type": "transcript.text.segment", "id": "s", "start": start, "end": end, "text": text,
            "speaker": speaker}


class PlanTest(unittest.TestCase):
    def test_one_window_when_the_audio_fits(self):
        self.assertEqual(pipeline_a.plan(900.0), [pipeline_a.Window(0, 0.0, 900.0, 0.0, 900.0)])

    def test_windows_overlap_and_split_each_overlap_in_half(self):
        windows = pipeline_a.plan(2160.0)
        self.assertEqual([(w.start, w.end, w.keep_from, w.keep_until) for w in windows],
                         [(0.0, 1200.0, 0.0, 1195.0), (1190.0, 2160.0, 1195.0, 2160.0)])

    def test_a_few_milliseconds_past_the_cap_stay_in_the_last_window(self):
        self.assertEqual([(w.start, w.end) for w in pipeline_a.plan(1200.4)], [(0.0, 1200.4)])

    def test_every_instant_is_kept_by_exactly_one_window(self):
        windows = pipeline_a.plan(5000.0, 1200.0, 10.0)
        for instant in (0.0, 1194.9, 1195.0, 2385.0, 4999.9):
            owners = [w for w in windows if w.keep_from <= instant < w.keep_until]
            self.assertEqual(len(owners), 1, instant)
            self.assertTrue(owners[0].start <= instant <= owners[0].end)


class CostTest(unittest.TestCase):
    def test_prices_duration_and_token_usage(self):
        usage = [{"type": "duration", "seconds": 600}, {"type": "tokens", "input_tokens": 1_000_000,
                                                         "output_tokens": 100_000}]
        self.assertAlmostEqual(pipeline_a._cost(usage), 0.06 + 2.5 + 1.0)
        self.assertEqual(pipeline_a._summed(usage)["seconds"], 600)


class MultipartTest(unittest.TestCase):
    def test_encodes_repeated_fields_and_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunk = Path(tmp) / "c.ogg"
            chunk.write_bytes(b"OggS-bytes")
            body, content_type = pipeline_a._multipart([("known_speaker_names[]", "S1"),
                                                        ("known_speaker_names[]", "S2")], chunk)
        message = Message()
        message["Content-Type"] = content_type
        self.assertEqual(body.count(b'name="known_speaker_names[]"'), 2)
        self.assertIn(b'filename="c.ogg"\r\nContent-Type: audio/ogg\r\n\r\nOggS-bytes\r\n', body)
        self.assertTrue(body.endswith(f"--{message.get_param('boundary')}--\r\n".encode()))


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TranscriberTest(unittest.TestCase):
    """50 s of audio in 20 s windows with 10 s overlap: windows at 0, 10, 20, 30; seams at 15, 25, 35."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "clip.opus"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=50", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
                        str(self.audio)], check=True)
        self.requests = []

    def tearDown(self):
        self.tmp.cleanup()

    def transcriber(self, *responses):
        queue = list(responses)

        def opener(request, timeout):
            self.requests.append(request)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return Reply(item, f"req_{len(self.requests)}")

        return pipeline_a.Transcriber(KEY, Path(self.tmp.name) / "work", opener=opener, ffmpeg=FFMPEG,
                                      sleep=lambda s: None, max_chunk_seconds=20.0)

    def run_clip(self, transcriber):
        return transcriber.transcribe(pipeline_a.Clip(self.audio, "ru-test", "ru", offset=100.0))

    def test_stitches_windows_carries_speakers_and_keeps_each_overlap_once(self):
        usage = {"type": "duration", "seconds": 20}
        doc = self.run_clip(self.transcriber(
            {"segments": [seg(0.0, 5.0, "A", "один"), seg(5.0, 12.0, "B", "два"), seg(12.0, 14.9, "A", "три"),
                          seg(15.0, 20.0, "B", "дубль")], "usage": usage},
            {"segments": [seg(5.0, 9.0, "S2", "четыре"), seg(9.0, 12.0, "S1", "пять"),
                          seg(12.0, 14.8, "A", "новый")], "usage": usage},
            {"segments": [seg(6.0, 9.0, "S3", "шесть"), seg(9.0, 14.0, "S2", "семь")], "usage": usage},
            {"segments": [seg(2.0, 4.0, "S2", "дубль"), seg(6.0, 10.0, "S1", "восемь")], "usage": usage},
        ))
        segments.validate(doc)
        self.assertEqual([(s["start"], s["speaker"], s["text"]) for s in doc["segments"]],
                         [(0.0, "S1", "один"), (5.0, "S2", "два"), (12.0, "S1", "три"), (15.0, "S2", "четыре"),
                          (19.0, "S1", "пять"), (22.0, "S3", "новый"), (26.0, "S3", "шесть"),
                          (29.0, "S2", "семь"), (36.0, "S1", "восемь")])
        self.assertEqual(doc["speakers"], ["S1", "S2", "S3"])
        self.assertEqual(doc["source"]["offset_s"], 100.0)
        self.assertEqual(doc["run"]["request_ids"], ["req_1", "req_2", "req_3", "req_4"])
        self.assertEqual(doc["run"]["usage"]["seconds"], 80)
        self.assertAlmostEqual(doc["run"]["cost_usd"], 0.008)

    def test_sends_references_named_by_video_wide_ids_and_never_logs_the_key(self):
        self.run_clip(self.transcriber(
            {"segments": [seg(0.0, 5.0, "A"), seg(5.0, 6.0, "B")]}, *[{"segments": []}] * 3))
        first, second = (r.data for r in self.requests[:2])
        self.assertNotIn(b"known_speaker_names", first)
        self.assertEqual(second.count(b'name="known_speaker_names[]"'), 1)
        self.assertIn(b"data:audio/wav;base64,", second)
        self.assertEqual(self.requests[0].get_header("Authorization"), f"Bearer {KEY}")
        work = Path(self.tmp.name) / "work"
        self.assertEqual(oct(work.stat().st_mode & 0o777), "0o700")
        self.assertFalse(any(KEY.encode() in p.read_bytes() for p in work.iterdir()))

    def test_a_fifth_speaker_before_a_seam_fails_the_item(self):
        crowd = {"segments": [seg(i * 3.0, i * 3.0 + 2.5, label) for i, label in enumerate("ABCDE")]}
        with self.assertRaisesRegex(pipeline_a.PipelineError, "5 speakers"):
            self.run_clip(self.transcriber(crowd))

    def test_retries_throttling_then_fails_on_a_permanent_error(self):
        throttled = urllib.error.HTTPError(pipeline_a.ENDPOINT, 429, "slow", Message(), None)
        denied = urllib.error.HTTPError(pipeline_a.ENDPOINT, 401, "no", Message(), None)
        with self.assertRaisesRegex(pipeline_a.PipelineError, "HTTP 401"):
            self.run_clip(self.transcriber(throttled, denied))
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
