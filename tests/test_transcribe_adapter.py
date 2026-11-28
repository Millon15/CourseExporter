import copy
import hashlib
import json
import unittest

from course_exporter.transcribe import adapter, d2_pins

MD5 = "0123456789abcdef0123456789abcdef"
VIDEO = adapter.Video(name="01 Вступ.MP4", drive_file_id="video-id", md5=MD5, duration_s=10.0)
STEM = f"01 Вступ.{MD5}.d2-nemotron3-parakeet3.{d2_pins.CONFIG8}-g0001"


def cue(start, end, speaker, text):
    return {"start": start, "end": end, "speaker": speaker, "text": text, "flags": [],
            "words": [{"start": start, "end": end, "text": text, "speaker": speaker, "flag": None}]}


def document(*segments):
    return {
        "schema": "uch-segments/1", "pipeline": "D2", "result_version": 1,
        "producer_config": d2_pins.PRODUCER_CONFIG, "language": "uk", "speakers": ["S1"],
        "source": {"clip_id": "job-1", "audio_sha256": "a" * 64, "offset_s": 0, "duration_s": 10.0},
        "segments": list(segments) or [cue(0.0, 2.5, "S1", "Привіт <усім> & всім"), cue(3.0, 4.0, None, "е")],
        "run": {"run_id": "run-1", "host": "Darwin arm64", "warm": False, "peak_rss_mb": 1.0,
                "stages_s": {"total": 1.0}, "request_ids": [], "usage": None, "cost_usd": None},
    }


def rendered(doc=None, generation=1):
    return {f.role: f for f in adapter.render(doc or document(), VIDEO, generation, {"provider": "kwiga"})}


class RenderTest(unittest.TestCase):
    def test_four_files_in_create_order_named_beside_the_video(self):
        files = adapter.render(document(), VIDEO, 1, {})
        self.assertEqual([(f.role, f.name) for f in files], [
            ("vtt", f"{STEM}.vtt"), ("text", f"{STEM}.txt"), ("segments", f"{STEM}.segments.json"),
            ("manifest", f"{STEM}.manifest.json")])
        self.assertTrue(all(f.sha256 == hashlib.sha256(f.data).hexdigest() for f in files))

    def test_segments_json_is_the_uch_shape_in_video_milliseconds_with_unknown_speakers_as_question_marks(self):
        payload = json.loads(rendered()["segments"].data)
        self.assertEqual(payload, {"schema_version": 1, "segments": [
            {"start": 0.0, "end": 2.5, "speaker": "S1", "text": "Привіт <усім> & всім"},
            {"start": 3.0, "end": 4.0, "speaker": "?", "text": "е"}]})

    def test_vtt_and_text_render_every_cue(self):
        files = rendered()
        self.assertEqual(files["vtt"].data.decode(), "WEBVTT\n\n"
                         "00:00:00.000 --> 00:00:02.500\n<v S1>Привіт &lt;усім&gt; &amp; всім</v>\n\n"
                         "00:00:03.000 --> 00:00:04.000\n<v ?>е</v>\n")
        self.assertEqual(files["text"].data.decode(), "[00:00:00] S1: Привіт <усім> & всім\n[00:00:03] ?: е\n")

    def test_manifest_binds_the_revision_and_the_output_hashes(self):
        files = rendered()
        manifest = json.loads(files["manifest"].data)
        self.assertEqual({k: manifest[k] for k in ("schema_version", "drive_file_id", "source_md5", "language",
                                                    "timing_kind", "speaker_method")},
                         {"schema_version": 1, "drive_file_id": "video-id", "source_md5": MD5, "language": "uk",
                          "timing_kind": "segment", "speaker_method": "nemotron-3-diarization-exclusive"})
        self.assertEqual(manifest["output_sha256s"], {r: files[r].sha256 for r in ("vtt", "text", "segments")})
        self.assertEqual(manifest["provenance"]["provider"], "kwiga")
        self.assertEqual(manifest["provenance"]["producer_config"], d2_pins.PRODUCER_CONFIG)

    def test_generation_padding_follows_uch(self):
        self.assertTrue(rendered(generation=12345)["vtt"].name.endswith("-g12345.vtt"))

    def test_a_zero_length_cue_widens_up_to_the_next_cue(self):
        doc = document(cue(1.0, 1.0, "S1", "То"), cue(1.05, 2.0, "S1", "есть"), cue(3.0, 3.0, "S1", "так"))
        payload = json.loads(rendered(doc)["segments"].data)
        self.assertEqual([(s["start"], s["end"]) for s in payload["segments"]], [(1.0, 1.05), (1.05, 2.0),
                                                                                (3.0, 3.2)])

    def test_an_empty_cue_joins_a_next_cue_at_the_same_millisecond(self):
        doc = document(cue(857.28, 857.76, None, "гнезно."), cue(857.76, 857.76, None, "То"),
                       cue(857.76, 859.68, "S1", "есть, типа."))
        video = adapter.Video("a.mp4", "id", MD5, 900.0)
        payload = json.loads({f.role: f for f in adapter.render(doc, video, 1, {})}["segments"].data)
        self.assertEqual(payload["segments"], [
            {"start": 857.28, "end": 857.76, "speaker": "?", "text": "гнезно."},
            {"start": 857.76, "end": 859.68, "speaker": "S1", "text": "То есть, типа."}])

    def test_an_empty_cue_with_a_known_other_speaker_never_joins(self):
        with self.assertRaises(adapter.Refused):
            adapter.render(document(cue(1.0, 1.0, "S1", "one"), cue(1.0, 2.0, "S2", "two")), VIDEO, 1, {})

    def test_the_manifest_counts_cue_repairs(self):
        doc = document(cue(1.0, 1.0, None, "То"), cue(1.0, 2.0, "S1", "есть"), cue(3.0, 3.0, "S1", "так"))
        manifest = json.loads(rendered(doc)["manifest"].data)
        self.assertEqual(manifest["provenance"]["cue_repairs"], {"joined": 1, "widened": 1})

    def test_refuses_what_uch_would_drop(self):
        long_speaker = document()
        long_speaker["segments"][0]["speaker"] = "S" * 129
        cases = {
            "empty at the video end": (document(cue(10.01, 10.01, "S1", "x")), 1),
            "after the video": (document(cue(9.0, 10.02, "S1", "x")), 1),
            "NUL": (document(cue(0.0, 1.0, "S1", "a\0b")), 1),
            "blank": (document(cue(0.0, 1.0, "S1", " ")), 1),
            "long speaker": (long_speaker, 1),
            "generation zero": (document(), 0),
            "generation too large": (document(), 2_147_483_648),
            "over 8 MiB": (document(cue(0.0, 1.0, "S1", "x" * (8 << 20))), 1),
        }
        for name, (doc, generation) in cases.items():
            with self.subTest(name), self.assertRaises(adapter.Refused):
                adapter.render(doc, VIDEO, generation, {})

    def test_refuses_an_uppercase_md5(self):
        with self.assertRaises(adapter.Refused):
            adapter.render(document(), adapter.Video("a.mp4", "id", MD5.upper(), 10.0), 1, {})

    def test_does_not_mutate_the_document(self):
        doc = document(cue(1.0, 1.0, None, "То"))
        before = copy.deepcopy(doc)
        adapter.render(doc, VIDEO, 1, {})
        self.assertEqual(doc, before)


class SelectionTest(unittest.TestCase):
    ROLES = ("vtt", "text", "segments", "manifest")

    def test_names_every_file_by_id_and_hash(self):
        ids = {r: f"id-{r}" for r in self.ROLES}
        shas = {r: hashlib.sha256(r.encode()).hexdigest() for r in self.ROLES}
        file = adapter.selection(VIDEO, 7, ids, shas)
        self.assertEqual(file.name, f"01 Вступ.{MD5}.selected.g0007.json")
        self.assertEqual(json.loads(file.data), {
            "schema_version": 1, "drive_file_id": "video-id", "source_md5": MD5, "generation": 7,
            "manifest": {"id": "id-manifest", "sha256": shas["manifest"]},
            "outputs": {r: {"id": f"id-{r}", "sha256": shas[r]} for r in ("vtt", "text", "segments")}})

    def test_refuses_a_missing_role(self):
        ids = {r: "id" for r in self.ROLES[:3]}
        with self.assertRaises(adapter.Refused):
            adapter.selection(VIDEO, 1, ids, {r: "a" * 64 for r in self.ROLES[:3]})

    def test_pattern_reads_this_revisions_generations_only(self):
        pattern = adapter.selection_pattern(VIDEO)
        self.assertEqual(pattern.fullmatch(f"01 Вступ.{MD5}.selected.g0012.json").group(1), "0012")
        self.assertIsNone(pattern.fullmatch(f"01 Вступ.{'f' * 32}.selected.g0012.json"))
        self.assertIsNone(pattern.fullmatch(f"01 Вступ.{MD5}.selected.g12.json"))


if __name__ == "__main__":
    unittest.main()
