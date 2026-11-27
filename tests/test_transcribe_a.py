import io
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
        self.assertEqual(pipeline_a.plan(900.0), [pipeline_a.Window(0, 0.0, 900.0, 0.0)])

    def test_windows_overlap_by_ten_seconds(self):
        self.assertEqual([(w.start, w.end, w.previous_end) for w in pipeline_a.plan(2160.0)],
                         [(0.0, 1200.0, 0.0), (1190.0, 2160.0, 1200.0)])

    def test_no_window_exceeds_the_cap_and_none_is_near_empty(self):
        for duration in (1200.0, 1200.4, 2390.0, 5000.0):
            with self.subTest(duration):
                windows = pipeline_a.plan(duration)
                self.assertTrue(all(w.end - w.start <= 1200.0 for w in windows))
                self.assertEqual(windows[-1].end, duration)
                self.assertTrue(all(w.end - w.start >= 10.0 for w in windows))


def placed(start, end, speaker="S1", text="одна фраза"):
    return {"start": start, "end": end, "speaker": speaker, "text": text, "flags": []}


WINDOW = pipeline_a.Window(1, 10.0, 30.0, 20.0)
"""Window 1 of `plan(30, 20, 10)`: it starts at 10 and the previous window ends at 20."""


def stitched(kept, incoming):
    return pipeline_a._stitch(kept, incoming, pipeline_a._seam_pairs(kept, incoming, WINDOW), WINDOW)


class StitchTest(unittest.TestCase):
    def test_keeps_one_copy_of_an_utterance_heard_by_both_windows(self):
        cases = {"jitter": ((13.0, 16.0), (14.0, 17.0)), "mirror": ((14.0, 17.0), (13.0, 16.0)),
                 "short pair across the cut": ((14.6, 15.1), (14.9, 15.4))}
        for name, (old, new) in cases.items():
            with self.subTest(name):
                joined = stitched([placed(*old)], [placed(*new)])
                self.assertEqual([s["flags"] for s in joined], [[]])

    def test_pairs_a_phrase_the_later_window_heard_only_in_part_and_flags_the_difference(self):
        kept = [placed(8.0, 14.0, text="один два три"), placed(14.1, 19.0, text="четыре пять")]
        incoming = [placed(10.0, 14.2, text="два три"), placed(14.2, 19.5, text="четыре пять"),
                    placed(19.5, 25.0, text="шесть")]
        self.assertEqual([(s["text"], s["flags"]) for s in sorted(stitched(kept, incoming), key=lambda s: s["start"])],
                         [("один два три", ["seam_unresolved"]), ("четыре пять", []), ("шесть", [])])

    def test_does_not_pair_distinct_short_turns_that_do_not_overlap(self):
        kept = [placed(14.0, 14.4, "S1", "Так")]
        incoming = [placed(15.4, 15.8, "S2", "Так")]
        self.assertEqual(pipeline_a._seam_pairs(kept, incoming, WINDOW), [])

    def test_flags_a_pair_whose_words_differ(self):
        joined = stitched([placed(14.0, 16.0, text="Не піднімай бюджет")], [placed(14.0, 16.0, text="Піднімай бюджет")])
        self.assertEqual([s["flags"] for s in joined], [["seam_unresolved"]])

    def test_keeps_and_flags_an_utterance_only_one_window_heard(self):
        joined = stitched([placed(12.0, 18.0)], [])
        self.assertEqual([(s["start"], s["flags"]) for s in joined], [(12.0, ["seam_unresolved"])])

    def test_flags_the_kept_side_of_a_contradiction(self):
        joined = stitched([placed(12.0, 18.0, text="альфа бета")], [placed(13.0, 19.0, text="гамма дельта")])
        self.assertEqual([(s["text"], s["flags"]) for s in joined], [("гамма дельта", ["seam_unresolved"])])

    def test_numbers_speakers_after_sorting_by_start(self):
        joined = [placed(14.0, 15.0, "S1"), placed(12.0, 19.0, "S2")]
        self.assertEqual([(s["start"], s["speaker"]) for s in pipeline_a._renumbered(joined)],
                         [(12.0, "S1"), (14.0, "S2")])


class LabelMapTest(unittest.TestCase):
    REFS = {"S1": Path("s1.wav"), "S2": Path("s2.wav")}

    def labels(self, kept, incoming, voices, known):
        pairs = pipeline_a._seam_pairs(kept, incoming, WINDOW)
        return pipeline_a._label_map(incoming, kept, pairs, voices, known, WINDOW)

    def test_a_new_label_paired_with_an_earlier_speaker_is_that_speaker(self):
        kept = [placed(0.0, 3.0, "S1", "начало"), placed(16.0, 18.0, "S1", "та же фраза")]
        incoming = [placed(16.0, 18.0, "A", "та же фраза"), placed(22.0, 23.0, "A", "потом")]
        self.assertEqual(self.labels(kept, incoming, {"S1": Path("s1.wav")}, ["S1"]), {"A": "S1"})

    def test_new_labels_continue_the_count(self):
        incoming = [placed(21.0, 22.0, "S1", "раз"), placed(23.0, 24.0, "A", "два"), placed(25.0, 26.0, "B", "три")]
        self.assertEqual(self.labels([placed(0.0, 5.0, "S1"), placed(5.0, 9.0, "S2")], incoming, self.REFS,
                                     ["S1", "S2"]), {"S1": "S1", "A": "S3", "B": "S4"})

    def test_ambiguous_mappings_fail_the_item(self):
        cases = {
            "unreferenced earlier speaker": ([placed(0.0, 1.0, "S1")], [placed(22.0, 23.0, "A")], {}, ["S1"]),
            "reference paired with another speaker": (
                [placed(0.0, 5.0, "S1"), placed(16.0, 18.0, "S1", "та же фраза")],
                [placed(16.0, 18.0, "S2", "та же фраза")], self.REFS, ["S1", "S2"]),
            "one label paired with two speakers": (
                [placed(12.0, 14.0, "S1", "первая фраза"), placed(16.0, 18.0, "S2", "вторая фраза")],
                [placed(12.0, 14.0, "A", "первая фраза"), placed(16.0, 18.0, "A", "вторая фраза")],
                self.REFS, ["S1", "S2"]),
            "two labels paired into one speaker": (
                [placed(12.0, 14.0, "S1", "первая фраза"), placed(16.0, 18.0, "S1", "вторая фраза")],
                [placed(12.0, 14.0, "A", "первая фраза"), placed(16.0, 18.0, "B", "вторая фраза")],
                {"S1": Path("s1.wav")}, ["S1"]),
        }
        for name, (kept, incoming, voices, known) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(pipeline_a.PipelineError, "ambiguous"):
                self.labels(kept, incoming, voices, known)


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
    """49.5 s of audio in 20 s windows with 10 s overlap: windows at 0, 10, 20 and 30."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "clip.opus"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=49.5", "-ac", "1", "-ar", "16000", "-c:a", "libopus",
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
            return Reply(item, f"req_{len(self.requests):08d}")

        return pipeline_a.Transcriber(KEY, Path(self.tmp.name) / "work", opener=opener, ffmpeg=FFMPEG,
                                      sleep=lambda s: None, max_chunk_seconds=20.0)

    def run_clip(self, transcriber):
        return transcriber.transcribe(pipeline_a.Clip(self.audio, "ru-test", "ru", offset=100.0))

    def test_stitches_windows_carries_speakers_and_keeps_each_overlap_once(self):
        usage = {"type": "duration", "seconds": 20}
        doc = self.run_clip(self.transcriber(
            {"segments": [seg(0.0, 5.0, "A", "один"), seg(5.0, 12.0, "B", "два"), seg(12.0, 14.9, "A", "три"),
                          seg(15.0, 20.0, "B", "четыре")], "usage": usage},
            {"segments": [seg(5.0, 9.0, "S2", "четыре"), seg(9.0, 12.0, "S1", "пять"),
                          seg(12.0, 14.8, "A", "новый")], "usage": usage},
            {"segments": [seg(6.0, 9.0, "S3", "шесть"), seg(9.0, 14.0, "S2", "семь")], "usage": usage},
            {"segments": [seg(2.0, 4.0, "S2", "семь"), seg(6.0, 10.0, "S1", "восемь")], "usage": usage},
        ))
        segments.validate(doc)
        self.assertEqual([(s["start"], s["speaker"], s["text"]) for s in doc["segments"]],
                         [(0.0, "S1", "один"), (5.0, "S2", "два"), (12.0, "S1", "три"), (15.0, "S2", "четыре"),
                          (19.0, "S1", "пять"), (22.0, "S3", "новый"), (26.0, "S3", "шесть"),
                          (29.0, "S2", "семь"), (36.0, "S1", "восемь")])
        self.assertEqual(doc["speakers"], ["S1", "S2", "S3"])
        self.assertEqual(doc["source"]["offset_s"], 100.0)
        self.assertEqual(doc["run"]["request_ids"], [f"req_{n:08d}" for n in range(1, 5)])
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

    def test_a_provider_error_reports_only_known_codes_and_well_formed_request_ids(self):
        cases = {
            "known": ({"message": f"Incorrect API key {KEY} for «урок»", "code": "invalid_api_key"}, "req_abc12345",
                      "OpenAI HTTP 401, request req_abc12345, error code invalid_api_key"),
            "echoed": ({"message": "no", "code": KEY}, KEY, "OpenAI HTTP 401, request invalid, error code other"),
        }
        for name, (error, request_id, expected) in cases.items():
            body = io.BytesIO(json.dumps({"error": error}).encode())
            denied = urllib.error.HTTPError(pipeline_a.ENDPOINT, 401, "no", {"x-request-id": request_id}, body)
            with self.subTest(name), self.assertRaises(pipeline_a.PipelineError) as caught:
                self.run_clip(self.transcriber(denied))
            self.assertEqual(str(caught.exception), expected)

    def test_retries_throttling_then_fails_on_a_permanent_error(self):
        throttled = urllib.error.HTTPError(pipeline_a.ENDPOINT, 429, "slow", Message(), None)
        denied = urllib.error.HTTPError(pipeline_a.ENDPOINT, 401, "no", Message(), None)
        with self.assertRaisesRegex(pipeline_a.PipelineError, "HTTP 401"):
            self.run_clip(self.transcriber(throttled, denied))
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
