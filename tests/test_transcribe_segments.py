import copy
import json
import tempfile
import unittest
from pathlib import Path

from course_exporter.transcribe import segments

DELETE = object()


def document():
    return {
        "schema": "uch-segments/1", "pipeline": "D", "producer_config": "d-test", "language": "uk",
        "speakers": ["S1", "S2"],
        "source": {"clip_id": "uk-smoke", "audio_sha256": "a" * 64, "offset_s": 0.0, "duration_s": 10.0},
        "segments": [
            {"start": 0.0, "end": 2.5, "speaker": "S1", "text": "Привіт <усім> & всім", "flags": [],
             "words": [{"start": 0.0, "end": 1.0, "text": "Привіт", "speaker": "S1", "flag": None}]},
            {"start": 3.0, "end": 4.0, "speaker": None, "text": "е", "flags": ["no_turn"],
             "words": [{"start": 3.0, "end": 4.0, "text": "е", "speaker": None, "flag": "no_turn"}]},
            {"start": 6.0, "end": 9.9, "speaker": "S2", "text": "Так.", "flags": []},
        ],
        "run": {"run_id": "r1", "host": "mac", "warm": False, "peak_rss_mb": 900.5,
                "stages_s": {"load": 1.0, "total": 3.0}, "request_ids": [], "usage": None, "cost_usd": None},
    }


def broken(path, value):
    doc = copy.deepcopy(document())
    target = doc
    for key in path[:-1]:
        target = target[key]
    if value is DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return doc



class ValidateTest(unittest.TestCase):
    def test_accepts_a_valid_document(self):
        segments.validate(document())

    def test_rejects_each_broken_rule(self):
        cases = {
            "schema": (["schema"], "uch-segments/2"),
            "pipeline": (["pipeline"], "C"),
            "language": (["language"], "en"),
            "sha256": (["source", "audio_sha256"], "xyz"),
            "duration": (["source", "duration_s"], 0),
            "order": (["segments", 2, "start"], 0.5),
            "span": (["segments", 0, "end"], -1.0),
            "after audio": (["segments", 2, "end"], 11.0),
            "empty text": (["segments", 0, "text"], " "),
            "null speaker without words": (["segments", 2, "speaker"], None),
            "unflagged word without speaker": (["segments", 0, "words", 0, "speaker"], None),
            "unknown flag": (["segments", 1, "words", 0, "flag"], "guess"),
            "speaker order": (["speakers"], ["S2", "S1"]),
            "missing speaker": (["speakers"], ["S1"]),
            "renumbered speakers": (["speakers"], ["S1", "S3"]),
            "warm": (["run", "warm"], "no"),
            "no total": (["run", "stages_s"], {"load": 1.0}),
            "unknown stage": (["run", "stages_s"], {"total": 1.0, "gpu": 1.0}),
            "bool as number": (["run", "cost_usd"], True),
            "no run": (["run"], DELETE),
        }
        for name, (path, value) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                segments.validate(broken(path, value))

    def test_rejects_a_name_as_speaker(self):
        doc = broken(["segments", 2, "speaker"], "Таня")
        doc["speakers"] = ["S1", "Таня"]
        with self.assertRaises(ValueError):
            segments.validate(doc)


class RenderTest(unittest.TestCase):
    def test_vtt_tags_voices_and_escapes_text(self):
        vtt = segments.to_vtt(document())
        self.assertTrue(vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:02.500\n"))
        self.assertIn("<v S1>Привіт &lt;усім&gt; &amp; всім</v>", vtt)
        self.assertIn("00:00:03.000 --> 00:00:04.000\nе\n", vtt)
        self.assertIn("00:00:06.000 --> 00:00:09.900\n<v S2>Так.</v>", vtt)

    def test_txt_lists_one_line_per_segment(self):
        self.assertEqual(segments.to_txt(document()).splitlines(),
                         ["[00:00:00] S1: Привіт <усім> & всім", "[00:00:03] ?: е", "[00:00:06] S2: Так."])

    def test_write_produces_three_utf8_files_and_refuses_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = segments.write(document(), Path(tmp), "clip")
            self.assertEqual([p.name for p in paths], ["clip.segments.json", "clip.vtt", "clip.txt"])
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), document())
            with self.assertRaises(ValueError):
                segments.write(broken(["pipeline"], "B"), Path(tmp), "bad")
            self.assertFalse((Path(tmp) / "bad.vtt").exists())

    def test_vtt_time_handles_hours(self):
        self.assertEqual(segments._vtt_time(3725.0506), "01:02:05.051")


if __name__ == "__main__":
    unittest.main()
