import unittest

from course_exporter.transcribe import d2, d2_pins


class PinsTest(unittest.TestCase):
    def test_producer_config_is_the_benchmarked_string(self):
        self.assertEqual(
            d2_pins.PRODUCER_CONFIG,
            "d2-nemotron3diar-52a43267-tf5241-mps-fp32-offline340r40-t05-argmax"
            "-parakeet-v3-ed2b7e8c-mlx070-bf16-a120-o15-seg5-p08-v1")
        self.assertRegex(d2_pins.CONFIG8, r"^[0-9a-f]{8}$")


class WordsFromTokensTest(unittest.TestCase):
    def test_joins_subword_pieces_on_whitespace_and_clamps_to_the_audio(self):
        tokens = [d2.Word(0.0, 0.2, " При"), d2.Word(0.2, 0.4, "віт,"), d2.Word(0.5, 0.7, " світ"),
                  d2.Word(9.9, 10.4, " кінець")]
        words = d2.words_from_tokens(tokens, duration=10.0)
        self.assertEqual(words, [d2.Word(0.0, 0.4, "Привіт,"), d2.Word(0.5, 0.7, "світ"),
                                 d2.Word(9.9, 10.0, "кінець")])

    def test_refuses_an_inverted_token(self):
        with self.assertRaises(ValueError):
            d2.words_from_tokens([d2.Word(1.0, 0.5, " x")], duration=10.0)


class ExclusiveTurnsTest(unittest.TestCase):
    def test_frames_go_to_their_strongest_speaker_above_the_threshold(self):
        best = [0.9, 0.8, 0.4, 0.7, 0.6, 0.6]
        speaker = [0, 0, 1, 1, 1, 0]
        turns = d2.exclusive_turns(best, speaker, frame_s=0.08)
        self.assertEqual([(round(t.start, 2), round(t.end, 2), t.speaker) for t in turns],
                         [(0.0, 0.16, "spk0"), (0.24, 0.4, "spk1"), (0.4, 0.48, "spk0")])


class JoinSegmentsTest(unittest.TestCase):
    def test_breaks_cues_on_speaker_punctuation_and_pause_and_flags_unturned_words(self):
        words = [d2.Word(0.0, 0.5, "Добрий"), d2.Word(0.5, 1.0, "день."), d2.Word(1.1, 1.5, "Так"),
                 d2.Word(1.6, 2.0, "ні"), d2.Word(3.0, 3.4, "е")]
        turns = [d2.Turn(0.0, 1.55, "spk3"), d2.Turn(1.55, 2.5, "spk1")]
        speakers, cues = d2.join_segments(words, turns)
        self.assertEqual(speakers, ["S1", "S2"])
        self.assertEqual([(c["start"], c["end"], c["speaker"], c["text"], c["flags"]) for c in cues], [
            (0.0, 1.0, "S1", "Добрий день.", []),
            (1.1, 1.5, "S1", "Так", []),
            (1.6, 2.0, "S2", "ні", []),
            (3.0, 3.4, None, "е", ["no_turn"]),
        ])

    def test_a_word_across_two_speakers_is_ambiguous(self):
        _, cues = d2.join_segments([d2.Word(0.9, 1.2, "так")], [d2.Turn(0, 1, "a"), d2.Turn(1, 2, "b")])
        self.assertEqual((cues[0]["speaker"], cues[0]["flags"]), (None, ["ambiguous"]))

    def test_a_cue_stops_before_five_seconds(self):
        words = [d2.Word(i * 0.6, i * 0.6 + 0.5, f"w{i}") for i in range(10)]
        _, cues = d2.join_segments(words, [d2.Turn(0, 10, "a")])
        self.assertTrue(all(c["end"] - c["start"] <= 5.0 for c in cues))
        self.assertEqual(len(cues), 2)


if __name__ == "__main__":
    unittest.main()
