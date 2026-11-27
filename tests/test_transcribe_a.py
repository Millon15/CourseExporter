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


class StitchTest(unittest.TestCase):
    """Window 1 of `plan(30, 20, 10)`: it starts at 10 and the previous window ends at 20."""

    WINDOW = pipeline_a.Window(1, 10.0, 30.0, 20.0)

    def test_keeps_one_copy_of_an_utterance_whose_timestamps_jitter_across_the_middle(self):
        for old, new in (((13.0, 16.0), (14.0, 17.0)), ((14.0, 17.0), (13.0, 16.0))):
            with self.subTest(old=old, new=new):
                joined = pipeline_a._stitch([placed(*old)], [placed(*new)], self.WINDOW)
                self.assertEqual(len(joined), 1)
                self.assertEqual(joined[0]["flags"], [])

    def test_cuts_at_a_gap_both_windows_share(self):
        kept = [placed(8.0, 14.0, text="a"), placed(14.1, 19.0, text="b")]
        incoming = [placed(10.0, 14.2, text="a2"), placed(14.2, 19.5, text="b2"), placed(19.5, 25.0, text="c")]
        self.assertEqual([s["text"] for s in pipeline_a._stitch(kept, incoming, self.WINDOW)], ["a", "b2", "c"])

    def test_flags_the_overlap_when_no_cut_is_free(self):
        kept = [placed(9.0, 20.5, text="long")]
        incoming = [placed(10.0, 16.0, text="x"), placed(16.0, 22.0, text="y")]
        joined = pipeline_a._stitch(kept, incoming, self.WINDOW)
        self.assertEqual([(s["text"], s["flags"]) for s in joined],
                         [("long", ["seam_unresolved"]), ("y", ["seam_unresolved"])])

    def test_numbers_speakers_after_sorting_by_start(self):
        joined = [placed(14.0, 15.0, "S1"), placed(12.0, 19.0, "S2")]
        self.assertEqual([(s["start"], s["speaker"]) for s in pipeline_a._renumbered(joined)],
                         [(12.0, "S1"), (14.0, "S2")])


class LabelMapTest(unittest.TestCase):
    WINDOW = pipeline_a.Window(1, 10.0, 30.0, 20.0)

    def test_an_unnamed_label_is_ambiguous_while_a_known_speaker_lacks_a_reference(self):
        raw = [seg(0.0, 3.0, "A")]
        with self.assertRaisesRegex(pipeline_a.PipelineError, "ambiguous"):
            pipeline_a._label_map(raw, {"S1": Path("r.wav")}, ["S1", "S2"], ["S2"], self.WINDOW)

    def test_named_labels_map_to_themselves_and_new_ones_continue_the_count(self):
        raw = [seg(0.0, 3.0, "S1"), seg(3.0, 6.0, "A"), seg(6.0, 9.0, "B")]
        labels = pipeline_a._label_map(raw, {"S1": Path("r.wav"), "S2": Path("q.wav")}, ["S1", "S2"], [], self.WINDOW)
        self.assertEqual(labels, {"S1": "S1", "A": "S3", "B": "S4"})


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

    def test_a_provider_error_reports_status_and_code_but_not_its_message(self):
        body = json.dumps({"error": {"message": f"Incorrect API key {KEY} for «урок»", "code": "invalid_api_key"}})
        denied = urllib.error.HTTPError(pipeline_a.ENDPOINT, 401, "no", {"x-request-id": "req_9"},
                                        io.BytesIO(body.encode()))
        with self.assertRaises(pipeline_a.PipelineError) as caught:
            self.run_clip(self.transcriber(denied))
        self.assertEqual(str(caught.exception), "OpenAI HTTP 401, request req_9, error code invalid_api_key")

    def test_retries_throttling_then_fails_on_a_permanent_error(self):
        throttled = urllib.error.HTTPError(pipeline_a.ENDPOINT, 429, "slow", Message(), None)
        denied = urllib.error.HTTPError(pipeline_a.ENDPOINT, 401, "no", Message(), None)
        with self.assertRaisesRegex(pipeline_a.PipelineError, "HTTP 401"):
            self.run_clip(self.transcriber(throttled, denied))
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
